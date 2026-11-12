"""本地存储：缓存最近一次抓取结果 + SQLite 成绩历史。"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from . import config

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS grades (id          INTEGER PRIMARY KEY AUTOINCREMENT,
    seen_at     TEXT NOT NULL,
    course      TEXT NOT NULL,
    name        TEXT NOT NULL,
    score       TEXT,
    max_score   TEXT,
    percent     REAL,
    gpa         REAL,
    category    TEXT,
    due_date    TEXT,
    submitted   INTEGER DEFAULT 0,
    overdue     INTEGER DEFAULT 0,
    url         TEXT,
    UNIQUE(course, name, due_date)
);
CREATE INDEX IF NOT EXISTS idx_grades_course ON grades(course);

CREATE TABLE IF NOT EXISTS snapshots (id          INTEGER PRIMARY KEY AUTOINCREMENT,
    taken_at    TEXT NOT NULL,
    overall_gpa REAL,
    payload     TEXT
);

CREATE TABLE IF NOT EXISTS gpa_daily (
    day         TEXT PRIMARY KEY,      -- YYYY-MM-DD，一天一条
    percent     REAL,                  -- 当天界面显示的百分制总评
    value       REAL,                  -- 同一份百分数按档位表换算的 4.0 值
    captured_at TEXT NOT NULL,         -- 最后一次写入时间
    rounds      INTEGER DEFAULT 1);    -- 当天写了多少次（刷新次数）
"""


# ---------- 通用：原子写 JSON ----------

def write_json_atomic(path, payload, *, backup: bool = True,
                      compact: bool = False) -> bool:
    """把 payload 原子地写成 JSON 文件。成功返回 True。

    ★ 为什么要统一走这里：
      `path.write_text(...)` 直接写会在「写一半被杀进程 / 断电 / 盘满」时
      留下**截断的 JSON**。而这类状态文件多数是「读完 → 改 → 写回」，
      下次启动读到截断文件又会被 except 兜成空，然后把这个空写回去 ——
      数据就这么静默没了（teams_crawl.json 的 121KB 历史就是这么暴露的）。

      做法：先写同目录临时文件 → 备份现有文件 → `os.replace` 原子替换。
      同目录很重要：跨盘的 replace 不是原子的。

    backup=False 时不留 .bak（用于可重算的临时产物）。
    compact=True 时输出紧凑 JSON（给浏览器 Preferences 这类
    原本就是紧凑格式的文件用，避免把文件撑大）。
    """
    p = Path(path)
    ok = False
    tmp = None
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f"{p.name}.tmp.{os.getpid()}.{threading.get_ident()}")
        if compact:
            text = json.dumps(payload, ensure_ascii=False,
                              separators=(",", ":"))
        else:
            text = json.dumps(payload, ensure_ascii=False, indent=1)
        tmp.write_text(text, encoding="utf-8")
        if backup and p.exists():
            try:
                p.replace(p.with_name(p.name + ".bak"))
            except Exception:
                pass
        os.replace(tmp, p)
        ok = True
    except Exception as e:
        log.warning("原子写入失败（%s）：%s", p.name, e)
    finally:
        #  失败时别留临时文件；成功时它已被 os.replace 移走
        try:
            if tmp is not None and tmp.exists():
                tmp.unlink()
        except Exception:
            pass
    return ok


# ---------- 缓存 ----------

def save_cache(payload: dict) -> None:
    """原子写入缓存：先写临时文件再替换，避免中途被杀导致文件损坏。

    同时保留一份 .bak 备份，防止意外丢失（曾有 cache.json 消失的情况）。
    """
    config.ensure_dirs()
    target = config.CACHE_FILE
    tmp = target.with_suffix(".tmp")
    text = json.dumps(payload, ensure_ascii=False, indent=2)

    try:
        tmp.write_text(text, encoding="utf-8")
        # 先备份现有文件
        if target.exists():
            try:
                target.replace(target.with_suffix(".json.bak"))
            except Exception:
                pass
        tmp.replace(target)
    except Exception as e:
        log.warning("写入缓存失败：%s", e)
        #  兜底：绕过临时文件直写。走到这里说明原子路径已经失败过一次，
        #  兜底再失败就**没有可用的缓存**了 —— 不能沉默（以前是 pass，
        #  于是「缓存写不进去」这件事在日志里完全看不到）。
        try:
            target.write_text(text, encoding="utf-8")
            log.warning("已用直写兜底保住了缓存（下次启动可能读到不完整内容）：%s",
                        target.name)
        except Exception as e2:
            log.error("缓存彻底写不进去（原子写与直写都失败）：%s", e2)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass


def load_cache() -> dict | None:
    """读取缓存；主文件损坏或缺失时回退到 .bak。"""
    for f in (config.CACHE_FILE, config.CACHE_FILE.with_suffix(".json.bak")):
        if not f.exists():
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(data, dict) and (data.get("tasks") or data.get("courses")):
                return data
        except Exception:
            continue
    return None


# ---------- 数据库 ----------

#  数据库健康状态：最近一次操作遇到的严重问题（"" = 正常）。
#  用途：成绩历史库是**唯一副本**，坏了必须让界面能如实说出来，
#        而不是只写一条 debug 日志（默认级别根本看不到）。
DB_HEALTH: dict = {"ok": True, "note": "", "checked_at": ""}


def _db_backup_daily() -> None:
    """每天给 grades.db 留一份 .bak（用 sqlite 自己的 backup API，热备安全）。

    为什么不用 copy：数据库可能有未落盘的 WAL/页缓存，直接复制文件
    可能拿到不一致的快照。`sqlite3.Connection.backup()` 是官方热备方式。
    """
    if not config.DB_FILE.exists():
        return
    day = datetime.now().strftime("%Y%m%d")
    marker = config.DB_FILE.with_name(f"grades.db.bak.{day}")
    if marker.exists():
        return                      # 今天已经备过
    try:
        #  先清掉更早的备份，避免 data/ 越攒越多
        for old in config.DB_FILE.parent.glob("grades.db.bak.*"):
            if old.name != marker.name:
                try:
                    old.unlink()
                except Exception:
                    pass
        src = sqlite3.connect(config.DB_FILE)
        try:
            dst = sqlite3.connect(marker)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        log.info("成绩库已备份：%s", marker.name)
    except Exception as e:
        log.warning("成绩库备份失败：%s", e)


def _connect() -> sqlite3.Connection:
    config.ensure_dirs()
    try:
        conn = sqlite3.connect(config.DB_FILE, timeout=5.0)
    except Exception as e:
        DB_HEALTH.update(ok=False, note=f"打不开成绩库：{e}",
                         checked_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        raise
    try:
        #  并发写时不至于立刻 "database is locked"
        conn.execute("PRAGMA busy_timeout = 5000")
    except Exception:
        pass
    try:
        conn.executescript(_SCHEMA)
    except Exception as e:
        #  建表都失败 = 库坏了（截断 / 非 sqlite 文件）。留下证据再抛，
        #  不要静默当成「没有历史」。
        try:
            conn.close()
        except Exception:
            pass
        DB_HEALTH.update(ok=False, note=f"成绩库结构异常：{e}",
                         checked_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        log.warning("成绩库不可用（%s）：%s", config.DB_FILE.name, e)
        raise
    _db_backup_daily()
    return conn


def _ensure_recall_column(conn) -> None:
    """给已有库补上 recalled / recalled_at 两列（幂等）。"""
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(grades)")}
        if "recalled" not in cols:
            conn.execute("ALTER TABLE grades ADD COLUMN "
                         "recalled INTEGER NOT NULL DEFAULT 0")
            log.info("成绩库迁移：已加 recalled 列")
        if "recalled_at" not in cols:
            conn.execute("ALTER TABLE grades ADD COLUMN recalled_at TEXT DEFAULT ''")
            log.info("成绩库迁移：已加 recalled_at 列")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_grades_recalled "
                     "ON grades(recalled)")
    except Exception as e:
        log.warning("成绩库迁移 recalled 列失败：%s", e)


#  最近一次抓取里被判定为「收回」的成绩 [(course, name), ...]
LAST_RECALLED: list = []


def _mark_recalled(conn, seen_by_course: dict, now: str) -> list:
    """把「本轮抓取里已经不存在」的成绩标记为召回。

    ★ 只在某门课**本轮确实抓到了任务**时才检查它 ——
      否则一次网络抖动 / 页面 401 会让整门课的成绩被误判成召回，
      那比不处理还糟糕（历史数据被抹掉）。
    """
    hit: list = []
    for course, names in seen_by_course.items():
        if not names:
            continue                      # 本轮没抓到这门课 → 不动
        try:
            rows = conn.execute(
                "SELECT id, name FROM grades "
                "WHERE course=? AND recalled=0 AND gpa IS NOT NULL",
                (course,)).fetchall()
        except Exception:
            continue
        for r in rows:
            #  ★ _connect() 没设 row_factory，这里是元组：
            #    SELECT id, name → r[0]=id, r[1]=name
            row_id, row_name = r[0], r[1]
            if row_name in names:
                continue
            try:
                conn.execute(
                    "UPDATE grades SET recalled=1, recalled_at=?, "
                    "gpa=NULL, percent=NULL, score='已收回（曾登记过成绩）' "
                    "WHERE id=?", (now, row_id))
                hit.append((course, row_name))
                log.info("成绩已收回：%s / %s（不再计入 GPA）",
                         course[:28], row_name[:30])
            except Exception as e:
                log.warning("标记成绩收回失败（%s）：%s", row_name, e)
    return hit


#  整门课连续没抓到的轮数：{course: n}
MISS_FILE = None          # 延迟到首次使用时按 config.DATA_DIR 计算
_miss_cache: dict | None = None
MISS_LIMIT = 3            # 连续这么多轮没抓到才认定收回


def _miss_path():
    from . import config
    return config.DATA_DIR / "course_misses.json"


def _load_misses() -> dict:
    global _miss_cache
    if _miss_cache is not None:
        return _miss_cache
    try:
        import json
        p = _miss_path()
        d = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
        _miss_cache = d if isinstance(d, dict) else {}
    except Exception:
        _miss_cache = {}
    return _miss_cache


def _save_misses(d: dict) -> None:
    global _miss_cache
    _miss_cache = dict(d)
    try:
        import json
        from . import config
        config.ensure_dirs()
        p = _miss_path()
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(p)
    except Exception as e:
        log.debug("存课程缺失计数失败：%s", e)


def _track_missing_courses(conn, seen_by_course: dict, now: str) -> list:
    """更新「整门课没抓到」的计数，够轮次就认定收回。"""
    if len(seen_by_course) < 5:
        #  本轮覆盖面太小（大面积失败），不做判定，避免集体误判
        return []
    try:
        rows = conn.execute(
            "SELECT DISTINCT course FROM grades "
            "WHERE recalled=0 AND gpa IS NOT NULL").fetchall()
        graded = {r[0] for r in rows}
    except Exception:
        return []

    misses = _load_misses()
    hit: list = []
    changed = False

    #  本轮抓到的课：计数清零
    for c in list(misses):
        if c in seen_by_course:
            misses.pop(c, None)
            changed = True

    missing = graded - set(seen_by_course)

    #  ★ 安全阀：缺失占比过高时**一律不判定** ★
    #    真实场景里「半门课没抓到」几乎都是登录态出问题 / 站点限速，
    #    不是课程被删。这种情况如果也累计轮次，几十分钟后就会把
    #    整批成绩误标成收回（实测演练时就踩过：10 门课全被标掉）。
    #    宁可漏判（保留旧数据），也不能误判。
    if missing and len(missing) * 3 > len(graded):
        log.warning("本轮有 %d/%d 门课没抓到，占比过高（疑似抓取不完整），"
                    "本次不做收回判定", len(missing), len(graded))
        return []

    #  本轮没抓到的课：计数 +1
    for c in missing:
        n = int(misses.get(c) or 0) + 1
        misses[c] = n
        changed = True
        if n < MISS_LIMIT:
            log.warning("课程「%s」连续 %d 轮没抓到（达到 %d 轮才认定为收回）",
                        c[:28], n, MISS_LIMIT)
            continue
        #  ★ 连续够轮次 → 认定收回 ★
        try:
            cur = conn.execute(
                "UPDATE grades SET recalled=1, recalled_at=?, "
                "gpa=NULL, percent=NULL, score='已收回（课程连续多轮抓不到）' "
                "WHERE course=? AND recalled=0 AND gpa IS NOT NULL",
                (now, c))
            n_row = cur.rowcount or 0
            if n_row:
                hit.append((c, "*"))
                log.warning("课程「%s」连续 %d 轮抓不到，已认定收回（%d 条成绩不再计入 GPA）",
                            c[:28], n, n_row)
        except Exception as e:
            log.warning("标记课程收回失败（%s）：%s", c[:28], e)

    if changed:
        _save_misses(misses)
    return hit


def record_grades(courses: list) -> int:
    """把本次抓到的**成绩**写入历史库；返回新增/更新的条数。

     修复说明（2026-09-20）：
      原来的实现遍历 `course.grades`，但我们的数据模型里**没有这个属性** ——
      成绩是存在**每个 Task** 上的（`task.score` / `task.score_num`）。
      所以这个函数从来没真正写入过，`grades` 表一直是空的
      （实测 0 行，而 snapshots 表有 269 行）。

      现在改成遍历 `course.tasks`，只记录**有成绩的**那些任务。
    """
    from datetime import date

    now = datetime.now().isoformat(timespec="seconds")
    today = date.today().isoformat()
    rows = 0

    #  本轮每门课「看到了哪些任务名」——用来判定成绩被收回
    seen_by_course: dict = {}

    with _connect() as conn:
        _ensure_recall_column(conn)
        for c in courses:
            names = seen_by_course.setdefault(c.name, set())
            for t in getattr(c, "tasks", []) or []:
                if (t.title or "").strip():
                    names.add(t.title.strip())
                # 只记录真的出了成绩的（没成绩的没意义）
                has_score = bool(t.score) and t.score != "N/A"
                if not (has_score or t.graded or t.score_num is not None):
                    continue

                # 计算百分制与 GPA（用统一的换算表）
                percent = t.percent
                gpa_val = None
                try:
                    from .gpa import parse_score

                    ps = parse_score(t.score or "", "")
                    if percent is None:
                        percent = ps.percent
                    gpa_val = ps.gpa
                except Exception:
                    pass

                due = (t.due_date or "")[:10] or today
                overdue = 0
                if t.days_left is not None and t.days_left < 0 and not t.completed:
                    overdue = 1

                try:
                    conn.execute("""
                        INSERT INTO grades
                            (seen_at, course, name, score, max_score, percent,
                             gpa, category, due_date, submitted, overdue, url)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(course, name, due_date) DO UPDATE SET
                            seen_at=excluded.seen_at,
                            score=excluded.score,
                            max_score=excluded.max_score,
                            percent=excluded.percent,
                            gpa=excluded.gpa,
                            submitted=excluded.submitted,
                            overdue=excluded.overdue,
                            --  ★ 曾经有分、这次变成没分 → 算「收回」，
                            --    保留痕迹但不再参与 GPA / 历史曲线计算
                            recalled=CASE
                                WHEN excluded.gpa IS NULL
                                     AND grades.gpa IS NOT NULL
                                THEN 1
                                --  ★ 成绩回来了 → 收回标记必须复位 ★
                                --    实测：课程恢复后 gpa 回来了，但 recalled
                                --    还停在 1，界面上仍显示「已收回」。
                                WHEN excluded.gpa IS NOT NULL THEN 0
                                ELSE grades.recalled END,
                            recalled_at=CASE
                                WHEN excluded.gpa IS NULL
                                     AND grades.gpa IS NOT NULL
                                THEN excluded.seen_at
                                WHEN excluded.gpa IS NOT NULL THEN ''
                                ELSE grades.recalled_at END
                        """,
                        (now,
                            c.name,
                            t.title,
                            t.score or "",
                            str(t.score_max) if t.score_max is not None else "",
                            percent,
                            gpa_val,
                            t.category or t.kind or "",
                            due,
                            int(bool(t.submitted)),
                            overdue,
                            t.url or "",
                        ),
                    )
                    rows += 1
                except Exception as e:
                    #  ★ 原来是 log.debug —— 默认 INFO 级别下完全看不到。
                    #    成绩库是唯一副本，写失败必须能被发现。
                    DB_HEALTH.update(ok=False, note=f"写入成绩失败：{e}",
                                     checked_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                    log.warning("写入成绩历史失败（%s）：%s", t.title, e)

    #  ★ 最后统一扫一遍「本轮消失的成绩」★
    with _connect() as conn:
        _ensure_recall_column(conn)
        hit = _mark_recalled(conn, seen_by_course, now)
        #  整门课没抓到：连续 3 轮才认定收回（单轮可能只是网络抖动）
        try:
            hit += _track_missing_courses(conn, seen_by_course, now)
        except Exception as e:
            log.debug("课程缺失跟踪失败：%s", e)
    LAST_RECALLED.clear()
    LAST_RECALLED.extend(hit)

    if rows:
        log.info("成绩历史：写入/更新 %d 条", rows)
    if hit:
        log.info("成绩历史：%d 条成绩已被收回，不再计入 GPA", len(hit))
    return rows


def record_snapshot(overall: float | None, payload: dict) -> None:
    with _connect() as conn:
        conn.execute("INSERT INTO snapshots (taken_at, overall_gpa, payload) VALUES (?,?,?)",
            (datetime.now().isoformat(timespec="seconds"), overall,
             json.dumps(payload, ensure_ascii=False)),
        )


def record_daily_gpa(percent: float | None = None,
                     value: float | None = None) -> None:
    """把「当天界面显示的数字」记成一条日记录。

    ★ 为什么要有这张表 ★

      以前所有历史数字都由成绩表反推。反推用的聚合方式与界面显示并不相同，
      同一天会出现两个数：界面显示 93.1%（4.00），图表算出 3.74。

      从现在起每天直接记下当天的数字 —— **一天一条，只记不推**。
      历史段仍由成绩推算（那是没有办法的办法），
      但要与记录段分开，各自用各自的数，不混在同一天里。

    同一刷新多次覆盖同一条（保留当天最后一次），并累加 rounds。

    ★★ value 必须由调用方传入「界面上那个 4.0 数字」★★

      曾经这里只收百分数，再内部调 percent_to_gpa() 换算 —— 那是错的：
      界面的 GPA 是「各科先各自换算成 4.0，再加权平均」，
      而 percent_to_gpa(平均百分数) 是「先平均，再按档位换算」。
      顺序不同结果就不同：93.1% 平均分 → 界面 3.71，
      而 percent_to_gpa(93.1) → 4.00。等于凭空把成绩抬高了。

      所以现在：显示的什么数就存什么数，一步换算都不做。
      percent 只作为同一时点的百分制参考一起存下来。
    """
    if percent is None and value is None:
        return
    pct = None
    try:
        if percent is not None:
            pct = float(percent)
    except (TypeError, ValueError):
        pct = None
    val = None
    try:
        if value is not None:
            val = round(float(value), 3)
    except (TypeError, ValueError):
        val = None
    if val is None:
        #  兼容：只有百分数时按档位表换算一次（调用方未提供显示值时）
        from .gpa import percent_to_gpa
        val = round(percent_to_gpa(pct), 3) if pct is not None else None
    if val is None:
        return
    val = max(0.0, min(4.0, val))

    day = datetime.now().strftime("%Y-%m-%d")
    with _connect() as conn:
        conn.execute(
            "INSERT INTO gpa_daily (day, percent, value, captured_at, rounds)"
            " VALUES (?,?,?,?,1)"
            " ON CONFLICT(day) DO UPDATE SET"
            "   percent=excluded.percent,"
            "   value=excluded.value,"
            "   captured_at=excluded.captured_at,"
            "   rounds=gpa_daily.rounds + 1",
            (day, pct, val, datetime.now().isoformat(timespec="seconds")),
        )


def daily_gpa_map() -> dict:
    """{日期: {"percent": .., "value": ..}} —— 每日记录（只读）。"""
    out: dict = {}
    try:
        with _connect() as conn:
            #  ★ 用位置下标：连接没有设置 row_factory，按列名取值会抛 TypeError，
            #    而这里又捕获了异常，表现就是「写进去了却读不出来」。
            for r in conn.execute(
                    "SELECT day, percent, value FROM gpa_daily ORDER BY day").fetchall():
                try:
                    out[str(r[0])] = {"percent": r[1], "value": r[2]}
                except (IndexError, TypeError):
                    continue
    except Exception:
        pass
    return out


def gpa_trend(course: str | None = None, limit: int = 40) -> list[tuple[str, float]]:
    """返回 (时间, 累积平均 GPA) 序列，用于画趋势线。"""
    with _connect() as conn:
        if course:
            cur = conn.execute("SELECT seen_at, gpa FROM grades WHERE course=? AND gpa IS NOT NULL"
                " ORDER BY seen_at ASC LIMIT ?", (course, limit))
        else:
            cur = conn.execute("SELECT seen_at, gpa FROM grades WHERE gpa IS NOT NULL"
                " ORDER BY seen_at ASC LIMIT ?", (limit,))
        rows = cur.fetchall()

    out: list[tuple[str, float]] = []
    acc: list[float] = []
    for seen_at, gpa in rows:
        acc.append(gpa)
        out.append((seen_at, round(sum(acc) / len(acc), 3)))
    return out


def db_path() -> Path:
    return config.DB_FILE


# ══════════ 数据清理（0.5.2）══════════
#
#  这项目从 0.3 一直用到现在，而 grades / snapshots 两张表
#  只增不减 —— 一学期下来会积到几万行。这里给个上限。
#
#  ★ 保留的是「最近」的，不是「最早」的 ——
#    趋势图看的是变化过程，旧到看不见就留着没用。

KEEP_GRADE_DAYS = 90        # 成绩明细留三个月
KEEP_GRADE_PER_COURSE = 200  # 每门课最多留这么多条
KEEP_SNAPSHOTS = 2000       # 总快照条数上限


def prune(verbose: bool = False) -> dict:
    """清掉过期的历史记录。返回 {grades, snapshots} 删掉多少条。

    安全设计：
      · 删之前先数一遍，删完再数一逼（写入失败时不会假报成功）
      · 任何异常都只记日志，不影响主流程
    """
    from datetime import timedelta

    out = {"grades": 0, "snapshots": 0}
    cutoff = (datetime.now() - timedelta(days=KEEP_GRADE_DAYS))
    cut_str = cutoff.isoformat(timespec="seconds")

    try:
        with _connect() as conn:
            # ① 超过保留期的
            cur = conn.execute(
                "DELETE FROM grades WHERE seen_at < ?", (cut_str,))
            out["grades"] += cur.rowcount or 0

            # ② 每门课只留最近 N 条
            cur = conn.execute(
                """DELETE FROM grades WHERE id NOT IN (
                     SELECT id FROM grades g2
                     WHERE g2.course = grades.course
                     ORDER BY g2.seen_at DESC LIMIT ?
                   )""", (KEEP_GRADE_PER_COURSE,))
            out["grades"] += cur.rowcount or 0

            # ③ 快照表同样只留最近 N 条
            cur = conn.execute(
                """DELETE FROM snapshots WHERE id NOT IN (
                     SELECT id FROM snapshots ORDER BY id DESC LIMIT ?
                   )""", (KEEP_SNAPSHOTS,))
            out["snapshots"] = cur.rowcount or 0
    except Exception as e:
        log.warning("清理历史记录失败（不影响使用）：%s", e)
        return out

    if out["grades"] or out["snapshots"]:
        log.info("历史清理：删了 %d 条成绩 / %d 条快照",
                 out["grades"], out["snapshots"])
    elif verbose:
        log.info("历史清理：无需清理")
    return out


def size_info() -> dict:
    """给「数据与备份」面板看：历史库多大、有多少行。

    由 widget.db_stats() 调用（界面启动体检 + 设置面板）。
    """
    out = {"bytes": 0, "grades": 0, "snapshots": 0,
           "oldest": "", "newest": ""}
    try:
        if config.DB_FILE.exists():
            out["bytes"] = config.DB_FILE.stat().st_size
        with _connect() as conn:
            out["grades"] = conn.execute(
                "SELECT COUNT(*) FROM grades").fetchone()[0]
            out["snapshots"] = conn.execute(
                "SELECT COUNT(*) FROM snapshots").fetchone()[0]
            r = conn.execute(
                "SELECT MIN(seen_at), MAX(seen_at) FROM grades").fetchone()
            if r:
                out["oldest"] = r[0] or ""
                out["newest"] = r[1] or ""
    except Exception as e:
        #  以前只写 debug（默认看不到）；读库失败是要能发现的
        DB_HEALTH.update(ok=False, note=f"读历史库统计失败：{e}",
                         checked_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        log.warning("读数据库大小失败：%s", e)
    return out
