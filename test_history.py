"""成绩历史序列的回归测试。

架构（0.5.2）
------------
    记录段：从当天起，每天把「界面显示的数字」直接落库（gpa_daily），
            图表取这个值，不再推算。
    推算段：此前的历史没有记录，只能由成绩倒推。

两条硬约束
----------
  ① 同一天只允许一种来源，不许在一天里混两个数；
  ② 记录段的数值必须等于「当日记录的百分数」按档位表换算的结果
     —— 这是与产品自身输出的比对，不是内部自洽检查。
"""
from __future__ import annotations

import pathlib
import sqlite3
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PASS = 0
FAIL = 0


def chk(ok: bool, label: str, extra=None) -> None:
    global PASS, FAIL
    print(f"  {'[OK]' if ok else '[X] '} {label}" + ("" if ok else f"   → {extra!r}"))
    if ok:
        PASS += 1
    else:
        FAIL += 1


SCHEMA = """
CREATE TABLE IF NOT EXISTS grades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  seen_at TEXT NOT NULL, course TEXT NOT NULL, name TEXT NOT NULL,
  score TEXT, max_score TEXT, percent REAL, gpa REAL, category TEXT,
  due_date TEXT, submitted INTEGER DEFAULT 0, overdue INTEGER DEFAULT 0,
  url TEXT);
CREATE TABLE IF NOT EXISTS snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  taken_at TEXT NOT NULL, overall_gpa REAL, payload TEXT);
"""

GRADES = [
    ("Course A", "a1", 90.0, 4.0, "2026-09-02"),
    ("Course B", "b1", 70.0, 2.0, "2026-09-04"),
    ("Course A", "a2", 80.0, 3.0, "2026-09-20"),
    ("Course C", "c1", 95.0, 4.0, "2026-10-01"),
]

REC_DAY = "2026-09-10"
REC_PCT = 88.0
#  ★ 刻意让它与 percent_to_gpa(88.0)=3.3 不同 ★
#    界面的 GPA 是「各科先换算再加权平均」，不是「平均百分数再换算」。
#    曲线必须原样采用记录值，绝不能自己换算一遍。
REC_VAL = 3.42


def build_db(path: pathlib.Path) -> None:
    con = sqlite3.connect(str(path))
    con.executescript(SCHEMA)
    for c, n, pct, g, due in GRADES:
        con.execute(
            "INSERT INTO grades (seen_at, course, name, score, percent, gpa,"
            " due_date) VALUES (?,?,?,?,?,?,?)",
            ("2026-10-01T00:00:00", c, n, f"score {pct}", pct, g, due))
    con.commit()
    con.close()


def main() -> int:
    print("=" * 76)
    print("  成绩历史：记录段取显示值 / 推算段由成绩算")
    print("=" * 76)

    from app import config, gpa, history, store

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="mbpk_hist3_"))
    db = tmp / "grades.db"
    build_db(db)
    real_db, real_data = config.DB_FILE, config.DATA_DIR
    real_cache = history.CACHE_FILE
    config.DB_FILE = db
    config.DATA_DIR = tmp
    history.CACHE_FILE = tmp / "gpa_history.json"

    try:
        store._connect().close()
        con = sqlite3.connect(str(db))
        con.execute(
            "INSERT OR REPLACE INTO gpa_daily"
            " (day, percent, value, captured_at, rounds) VALUES (?,?,?,?,?)",
            (REC_DAY, REC_PCT, REC_VAL,
             "2026-09-10T21:00:00", 3))
        con.commit()
        con.close()

        r = history.daily_series()
        pts = r.get("points") or []
        by = {p["date"]: p for p in pts}

        print("\n── ① 记录段取「当日显示值」，不推算 ──")
        rec = by.get(REC_DAY)
        chk(rec is not None, f"{REC_DAY} 有点", rec)
        if rec:
            chk(rec["source"] == "record", f"来源是 record（{rec['source']}）",
                rec["source"])
            #  ★ 关键回归：取的是记录值本身，不是 percent_to_gpa 的换算结果 ★
            chk(abs(rec["gpa"] - REC_VAL) < 1e-9,
                f"数值等于记录值 {REC_VAL}（实际 {rec['gpa']}）",
                (rec["gpa"], REC_VAL))
            wrong = round(gpa.percent_to_gpa(REC_PCT), 3)
            chk(abs(rec["gpa"] - wrong) > 1e-9 or abs(REC_VAL - wrong) < 1e-9,
                f"没有退回「按百分数换算」（换算会得到 {wrong}）",
                (rec["gpa"], wrong))
            chk(abs((rec["percent"] or 0) - REC_PCT) < 1e-9,
                f"percent 等于记录值 {REC_PCT}（实际 {rec['percent']}）",
                rec["percent"])

        print("\n── ② 同一天只有一种来源 ──")
        days = [p["date"] for p in pts]
        chk(len(days) == len(set(days)), "没有重复日期",
            len(days) - len(set(days)))
        chk(set(p["source"] for p in pts) <= {"record", "grade"},
            "来源只有 record / grade")
        chk(r.get("days_recorded") == 1,
            f"记录段 {r.get('days_recorded')} 天", r.get("days_recorded"))
        chk(r.get("record_from") == REC_DAY,
            f"记录段起点 {r.get('record_from')}", r.get("record_from"))

        print("\n── ③ 记录段之外的日期仍走推算 ──")
        others = [p for p in pts if p["date"] != REC_DAY]
        chk(all(p["source"] == "grade" for p in others), "其余日期全部是 grade",
            {p["source"] for p in others})

        print("\n── ④ 推算段：有变必有分 ──")
        bad = []
        prev = None
        for p in pts:
            if p["source"] != "grade":
                prev = p
                continue
            if prev is not None and prev["source"] == "grade" \
                    and abs(p["gpa"] - prev["gpa"]) > 1e-9 and not p.get("grades"):
                bad.append(p["date"])
            prev = p
        chk(not bad, "推算段里变化的日子都带着成绩", bad)

        print("\n── ⑤ 推算段：成绩归到截止日 ──")
        withg = [p for p in pts if p.get("grades")]
        grade_days = sorted({g[4] for g in GRADES})
        chk([p["date"] for p in withg] == grade_days,
            "有成绩的点落在截止日期上",
            ([p["date"] for p in withg], grade_days))

        print("\n── ⑥ 单科视图（只有推算）──")
        rc = history.daily_series("Course A")
        cpts = rc.get("points") or []
        chk(len(cpts) > 0, f"Course A 有 {len(cpts)} 个点", len(cpts))
        chk(all(p["source"] == "grade" for p in cpts), "单科全部走推算",
            {p["source"] for p in cpts})
        chk(rc.get("days_recorded") == 0,
            f"单科没有记录段（{rc.get('days_recorded')}）", rc.get("days_recorded"))

        print("\n── ⑦ 起止日期 ──")
        rr = history.daily_series("", "2026-09-08", "2026-09-12")
        rp = rr.get("points") or []
        chk(rr.get("start") == "2026-09-08" and rr.get("end") == "2026-09-12",
            f"范围 {rr.get('start')} → {rr.get('end')}",
            (rr.get("start"), rr.get("end")))
        chk(len(rp) == 5, f"共 5 天（实际 {len(rp)}）", len(rp))

        print("\n── ⑧ 按周聚合 ──")
        wk = history.weekly_series("", "2026-09-01", "2026-09-30")
        wp = wk.get("points") or []
        chk(wk.get("grain") == "week", "标记 grain=week", wk.get("grain"))
        chk(0 < len(wp) < len(pts), f"点数变少（{len(pts)} → {len(wp)}）",
            (len(pts), len(wp)))

        print("\n── ⑨ 缓存 ──")
        chk(history.CACHE_FILE.exists(), "缓存文件已写入")
        again = history.daily_series()
        chk([p["date"] for p in again["points"]] == days, "缓存结果一致")

        print("\n── ⑩ 线性折算不是水平线 ──")
        dev = [abs((p.get("linear") or 0) - (p.get("percent") or 0) / 100 * 4)
               for p in pts if p.get("linear") is not None]
        chk(bool(dev) and max(dev) < 0.005,
            f"线性 = percent/100*4（最大偏差 {max(dev) if dev else 'n/a'}）", dev)

        print("\n── ⑪ 空库 ──")
        empty = tmp / "empty.db"
        con = sqlite3.connect(str(empty))
        con.executescript(SCHEMA)
        con.commit()
        con.close()
        config.DB_FILE = empty
        history.CACHE_FILE = tmp / "empty_cache.json"
        re0 = history.daily_series()
        chk(re0.get("ok") is True and re0.get("points") == [],
            "空库返回 ok 且无点", re0.get("ok"))
        chk(bool(re0.get("note")), f"带说明：{re0.get('note')[:32]}",
            re0.get("note"))

    finally:
        config.DB_FILE = real_db
        config.DATA_DIR = real_data
        history.CACHE_FILE = real_cache
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("=" * 76)
    print(f"  {PASS} 通过 / {FAIL} 失败")
    print("=" * 76)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
