"""成绩历史序列（K 线 / 曲线用）。

为什么要另写一个
----------------
`store.gpa_trend()` 不能用：
  · 它按 `seen_at` 排序 —— 那是「最后见到」的时间（UPSERT 会覆盖），
    不是出分时间，画出来的曲线顺序是错的
  · `LIMIT 40` 直接截断
  · 没有按日聚合，一天多个快照会重复计数
  · 没有区分 4.0 制与百分制

数据来源与诚实边界
------------------
两段：
  ① **真实段**（有快照的日期）：`snapshots` 表按日取当天最后一条。
     它记录的是「当天那一刻算出来的总平均分」，是实测值。
  ② **回算段**（早于第一个快照的日期）：按成绩的**截止日期**逐日累加。
     程序是 2026-09-24 才第一次运行的，9-01 ~ 9-23 没有任何快照，
     要覆盖用户要求的「从 9.1 开始」只能回算。

     回算用截止日期而不是实际出分日期 —— 老师可能晚几天登分，
     所以这一段是**近似**，返回里用 source="recalc" 标出，界面必须区分显示。

单位
----
`grades.gpa` 列实测就是 4.0 制（percent 96.0 → gpa 4.0），
`snapshots.overall_gpa` 存的却是**百分制平均分**（90.63）。
两者不能混用，本模块统一输出 4.0 制，并在内部把快照值换算过来。
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime, timedelta

from . import config, gpa, store

log = logging.getLogger(__name__)

#  曲线最多画多少个点（一天一个点，400 天足够一整学年）
MAX_POINTS = 400

#  曲线缓存（全量算一次就存下来，签名不变直接读）





def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(config.DB_FILE), timeout=8)
    conn.row_factory = sqlite3.Row
    return conn




def _iso_days(start: date, end: date) -> list[str]:
    """闭区间内的每一天（ISO 字符串）。"""
    out: list[str] = []
    d = start
    while d <= end and len(out) < MAX_POINTS:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out




def _safe_float(v):
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None




def _snapshot_days() -> dict:
    """{日期: 当天最能代表「抓全了」的那个快照的 4.0 值}。

    ★★ 为什么不能简单取「当天最后一条」★★

      实测发现快照是在**抓取过程中**就写下的：同一天里能看到
      84.52 → 85.56 → 85.62 这样的连续变化，而同时 `tasks` 从 52 涨到 56
      —— 说明抓了一半就记了一次。若直接取当天最后一条，
      取到哪个值取决于当时抓到几门课，曲线会凭空抖动。

      所以按天分组后先看 `payload.tasks`（那次抓到了多少任务）：
      取任务数**最多**的那一批（= 抓得最全的），再取其中最后一条。
      这样一天一个值，且是「当天最完整的那次观测」。

    快照里存的是百分制（90.63），这里统一换成 4.0。
    """
    import json as _json

    #  {日期: (tasks, taken_at, pct)}
    best: dict = {}
    try:
        with _connect() as conn:
            cur = conn.execute(
                "SELECT taken_at, overall_gpa, payload FROM snapshots "
                "WHERE overall_gpa IS NOT NULL ORDER BY taken_at ASC")
            for row in cur.fetchall():
                ts = str(row["taken_at"] or "")
                if len(ts) < 10:
                    continue
                pct = _safe_float(row["overall_gpa"])
                if pct is None:
                    continue
                tasks = 0
                try:
                    payload = _json.loads(row["payload"] or "{}")
                    tasks = int(payload.get("tasks") or 0)
                except Exception:
                    tasks = 0
                #  同一天里优先取「抓到最多任务」的那次（抓全了的那个）
                day = ts[:10]
                prev = best.get(day)
                if prev is None or tasks > prev[0] or (
                        tasks == prev[0] and ts >= prev[1]):
                    best[day] = (tasks, ts, pct)
    except Exception as e:
        log.debug("读快照失败：%s", e)

    out: dict = {}
    for day, (_tasks, _ts, pct) in best.items():
        out[day] = round(gpa.percent_to_gpa(pct), 3)
    return out




def _grades(course: str | None = None) -> list[dict]:
    """按截止日期升序取成绩（只取有 4.0 值的）。"""
    rows: list[dict] = []
    try:
        with _connect() as conn:
            sql = ("SELECT course, name, score, percent, gpa, due_date, seen_at "
                   "FROM grades WHERE gpa IS NOT NULL")
            args: tuple = ()
            if course:
                sql += " AND course = ?"
                args = (course,)
            sql += " ORDER BY due_date ASC, id ASC"
            for r in conn.execute(sql, args).fetchall():
                d = str(r["due_date"] or "")[:10]
                g = _safe_float(r["gpa"])
                if not d or g is None:
                    continue
                rows.append({
                    "course": str(r["course"] or ""),
                    "name": str(r["name"] or ""),
                    "score": str(r["score"] or ""),
                    "percent": _safe_float(r["percent"]),
                    "gpa": g,
                    "date": d,
                })
    except Exception as e:
        log.debug("读成绩失败：%s", e)
    return rows


CACHE_FILE = config.DATA_DIR / "gpa_history.json"


#  每个点的「当天成绩」最多列几条（多了放不下）
MAX_DAY_GRADES = 8




def _grade_rows_by_day() -> dict:
    """{日期: [{course, name, score, percent, gpa}, ...]}（按截止日期归日）。

    ★ 为什么用截止日期
      这是唯一可靠的「这条成绩属于哪一天」的线索。真正出分的时刻没记
      （seen_at 是最后见到时间，会被覆盖），所以这里如实按截止日期归日，
      并在界面上说明是「按截止日期」。
    """
    out: dict = {}
    try:
        with _connect() as conn:
            cur = conn.execute(
                "SELECT course, name, score, percent, gpa, due_date "
                "FROM grades WHERE gpa IS NOT NULL "
                "ORDER BY due_date ASC, id ASC")
            for r in cur.fetchall():
                day = str(r["due_date"] or "")[:10]
                if not day:
                    continue
                out.setdefault(day, []).append({
                    "course": str(r["course"] or ""),
                    "name": str(r["name"] or ""),
                    "score": str(r["score"] or ""),
                    "percent": _safe_float(r["percent"]),
                    "gpa": round(float(r["gpa"]), 3),
                })
    except Exception as e:
        log.debug("读每日成绩明细失败：%s", e)
    return out


#  ★ 算法规格版本 ★ 改口径必须加一，否则旧缓存签名不变会被原样复用
#  v4：签名纳入「已收回」计数 —— 成绩收回后必须让旧曲线缓存失效
SCHEMA_VERSION = 4




def _signature() -> str:
    """缓存签名：算法版本 + 成绩条数 + 最后一条截止日 + 快照范围。"""
    try:
        with _connect() as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM grades").fetchone()[0]
            #  ★ 收回的成绩必须体现在签名里 ★
            #    收回是 UPDATE（recalled=1、gpa 置空），**总行数不变**，
            #    只看 COUNT(*) 的话签名不变，旧曲线缓存会一直被命中，
            #    界面上继续显示收回前的结果（实测踩过）。
            try:
                rec = conn.execute(
                    "SELECT COUNT(*) FROM grades WHERE recalled=1"
                ).fetchone()[0]
            except Exception:
                #  旧库 / 测试临时库可能还没有 recalled 列。
                #  ★ 这里绝不能抛 ★ —— 抛了 _signature() 会返回空串，
                #    而空签名会让 _cache_save 直接跳过，曲线缓存悄悄失效。
                rec = 0
            n_graded = conn.execute(
                "SELECT COUNT(*) FROM grades WHERE gpa IS NOT NULL"
            ).fetchone()[0]
            last = conn.execute(
                "SELECT MAX(due_date) FROM grades").fetchone()[0] or ""
            snap = conn.execute(
                "SELECT COUNT(*), MAX(taken_at) FROM snapshots").fetchone()
            dly = conn.execute(
                "SELECT COUNT(*), MAX(day) FROM gpa_daily").fetchone()
            return (f"v{SCHEMA_VERSION}|{n}|{rec}|{n_graded}|{last}|"
                    f"{snap[0]}|{snap[1]}|{dly[0]}|{dly[1]}")
    except Exception:
        return ""




def _cache_load(sig: str, course: str, start: str, end: str):
    if not sig:
        return None
    try:
        import json as _json
        if not CACHE_FILE.exists():
            return None
        d = _json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        if (d.get("sig") != sig
                or d.get("course", "") != (course or "")):
            return None
        if d.get("start") != (start or "") or d.get("end") != (end or ""):
            return None
        return d.get("payload")
    except Exception:
        return None




def _cache_save(sig: str, course: str, start: str, end: str, payload) -> None:
    if not sig:
        return
    try:
        import json as _json
        config.ensure_dirs()
        tmp = CACHE_FILE.with_suffix(".json.tmp")
        tmp.write_text(
            _json.dumps({
                "sig": sig,
                "course": course or "",
                "start": start or "",
                "end": end or "",
                "payload": payload,
            }, ensure_ascii=False),
            encoding="utf-8")
        tmp.replace(CACHE_FILE)
    except Exception as e:
        log.debug("写历史曲线缓存失败：%s", e)




def _attach_day_grades(points: list) -> None:
    """给每个点补上「当天获得哪几个成绩」。"""
    if not points:
        return
    by_day = _grade_rows_by_day()
    for p in points:
        gs = by_day.get(p["date"]) or []
        if not gs:
            continue
        p["grades"] = gs[:MAX_DAY_GRADES]
        p["grades_total"] = len(gs)




def daily_series(course: str | None = None,
                 start: str = "", end: str = "") -> dict:
    """逐日 GPA 序列。

    ★ 一条线，两种来源，按日划分，互不混用 ★

        有「每日记录」的日子（2026-11-02 起）
            → 直接取当天界面显示的数字（store.record_daily_gpa 写入）
            → source = "record"

        没有记录的日子（此前的历史）
            → 由成绩表推算：各科百分数均值 → 档位表换 4.0 → 等权平均
            → source = "grade"

    这样处理的原因：
      · 历史段无法回溯「当天显示过什么」，只能推算；
      · 从今天起每天把显示值直接落库，不需要再推算，也不会与界面打架；
      · 同一天只用一种来源，不在一天里混两个数。

    历史段的推算值与当时界面显示可能不同，这是已知且接受的：
    推算用的聚合方式与学校口径不同。不做解释，也不试图对齐。
    """
    sig = _signature()
    cached = _cache_load(sig, course, start, end)
    if cached:
        return cached

    rows = _grades(course or None)

    #  每日实际记录只在看总览时用（单科没有这条记录）
    daily = {} if course else store.daily_gpa_map()

    all_dates = sorted({r["date"] for r in rows} | set(daily))
    if not all_dates:
        return {
            "ok": True, "course": course or "", "unit": "4.0",
            "points": [], "total": 0, "has_record": False,
            "course_names": _course_names(),
            "record_from": "", "record_to": "",
            "computed_from": "", "computed_to": "",
            "days_recorded": 0, "days_computed": 0,
            "note": "还没有任何成绩记录，无法生成历史曲线。",
        }

    rows.sort(key=lambda r: r["date"])
    first_data, last_data = all_dates[0], all_dates[-1]
    today = date.today().isoformat()

    #  ★ 默认起点取最早数据所在月的 1 号（覆盖「从 9.1 开始」这种整月起点）
    default_start = first_data
    try:
        default_start = date.fromisoformat(first_data).replace(day=1).isoformat()
    except ValueError:
        pass
    try:
        d_start = (date.fromisoformat(start) if start
                   else date.fromisoformat(default_start))
    except ValueError:
        d_start = date.fromisoformat(default_start)
    try:
        d_end = (date.fromisoformat(end) if end
                 else date.fromisoformat(min(today, last_data)))
    except ValueError:
        d_end = date.fromisoformat(min(today, last_data))
    if d_end < d_start:
        d_start, d_end = d_end, d_start

    days = _iso_days(d_start, d_end)

    #  ★ 当天成绩明细必须从**本次的 rows** 建，不能用全量：
    #    单科曲线（course 非空）时 rows 已按科目过滤，
    #    用全量会把别的科目的成绩挂到这条曲线上。
    by_day: dict = {}
    for r in rows:
        by_day.setdefault(r["date"], []).append(r)

    points: list[dict] = []
    per_course: dict = {}
    per_course_pct: dict = {}
    cursor = 0
    rec_days: list[str] = []
    cmp_days: list[str] = []

    for day in days:
        #  把截至当天的成绩并进累计集合
        while cursor < len(rows) and rows[cursor]["date"] <= day:
            r = rows[cursor]
            per_course.setdefault(r["course"], []).append(r["gpa"])
            if r["percent"] is not None:
                per_course_pct.setdefault(r["course"], []).append(r["percent"])
            cursor += 1

        todays = by_day.get(day, [])
        rec = daily.get(day)

        if rec is not None:
            #  ── 实际记录段：界面上当天显示的数字，不再换算 ──
            val = rec.get("value")
            pct = rec.get("percent")
            if val is None and pct is not None:
                val = round(gpa.percent_to_gpa(pct), 3)
            if val is None:
                continue
            src = "record"
            rec_days.append(day)
        else:
            #  ── 推算段：各科先求均值，再等权平均 ──
            if not per_course:
                continue
            means = [sum(v) / len(v) for v in per_course.values() if v]
            if not means:
                continue
            pct_means = [sum(v) / len(v) for v in per_course_pct.values() if v]
            if pct_means:
                pct = sum(pct_means) / len(pct_means)
                val = round(sum(gpa.percent_to_gpa(x) for x in pct_means)
                            / len(pct_means), 3)
            else:
                val = round(sum(means) / len(means), 3)
                pct = gpa.gpa_to_percent(val)
            src = "grade"
            cmp_days.append(day)

        pt = {
            "date": day,
            "gpa": round(float(val), 3),
            "percent": (round(float(pct), 2) if pct is not None else None),
            "linear": (round(float(pct) / 100 * 4, 3)
                       if pct is not None else None),
            "source": src,
            "carried": False,
            "new_grades": len(todays),
            "grades_total": len(todays),
        }
        if todays:
            pt["grades"] = [
                {
                    "course": t["course"],
                    "name": t["name"],
                    "score": t["score"],
                    "percent": t["percent"],
                    "gpa": round(t["gpa"], 3),
                }
                for t in todays[:MAX_DAY_GRADES]
            ]
        points.append(pt)

    #  ── 自检：推算段里「变了却没有当天成绩」必须为 0 ──
    violations = 0
    prev = None
    for p in points:
        if p["source"] != "grade":
            prev = p
            continue
        if (prev is not None and prev["source"] == "grade"
                and abs(p["gpa"] - prev["gpa"]) > 1e-9
                and not p.get("grades")):
            violations += 1
        prev = p

    #  记录段的同类自检（记录是界面数字，允许与成绩不同步）
    rec_changed_no_grade = 0
    prev = None
    for p in points:
        if p["source"] == "record":
            if (prev is not None
                    and abs(p["gpa"] - prev["gpa"]) > 1e-9
                    and not p.get("grades")):
                rec_changed_no_grade += 1
        prev = p

    if rec_days and cmp_days:
        note = (f"{rec_days[0]} 起为**每日实际记录**（当天界面显示的数字）；"
                f"{cmp_days[0]} ~ {cmp_days[-1]} 为**由成绩推算**。"
                f"两段的取数方式不同，历史推算值可能与当时的显示值不一致。")
    elif rec_days:
        note = f"全部为每日实际记录（{rec_days[0]} 起）。"
    else:
        note = "全部由成绩推算（尚无每日记录）。推算值与当时界面显示可能不一致。"

    result = {
        "ok": True,
        "course": course or "",
        "start": days[0] if days else "",
        "end": days[-1] if days else "",
        "unit": "4.0",
        "points": points,
        "total": len(points),
        "has_record": bool(rec_days),
        "record_from": rec_days[0] if rec_days else "",
        "record_to": rec_days[-1] if rec_days else "",
        "computed_from": cmp_days[0] if cmp_days else "",
        "computed_to": cmp_days[-1] if cmp_days else "",
        "days_recorded": len(rec_days),
        "days_computed": len(cmp_days),
        "first_data": first_data,
        "last_data": last_data,
        "rule_violations": violations,
        "record_changed_without_grade": rec_changed_no_grade,
        "course_names": _course_names(),
        "note": note,
    }
    _cache_save(sig, course, start, end, result)
    return result




def _course_names() -> list:
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT course FROM grades WHERE course IS NOT NULL "
                "AND course != '' ORDER BY course").fetchall()
            return [str(r["course"]) for r in rows]
    except Exception:
        return []




def weekly_series(course: str | None = None,
                  start: str = "", end: str = "") -> dict:
    """按 ISO 周（周一起）聚合的同一份数据。

    每周取该周最后一个有值的点；没有点的周直接跳过（不补零）。
    """
    d = daily_series(course, start, end)
    if not d.get("points"):
        return d

    weeks: dict = {}
    for p in d["points"]:
        try:
            dt = datetime.fromisoformat(p["date"]).date()
        except ValueError:
            continue
        monday = (dt - timedelta(days=dt.weekday())).isoformat()
        #  后出现的覆盖前面的（每周取最后一个）
        weeks[monday] = dict(p, date=monday, week_of=p["date"])

    pts = [weeks[k] for k in sorted(weeks.keys())]
    out = dict(d)
    out["points"] = pts
    out["total"] = len(pts)
    out["grain"] = "week"
    return out
