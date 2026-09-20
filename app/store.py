"""本地存储：缓存最近一次抓取结果 + SQLite 成绩历史。"""
from __future__ import annotations

import json
import logging
import sqlite3
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
"""


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
        import logging

        logging.getLogger(__name__).warning("写入缓存失败：%s", e)
        try:
            target.write_text(text, encoding="utf-8")
        except Exception:
            pass
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

def _connect() -> sqlite3.Connection:
    config.ensure_dirs()
    conn = sqlite3.connect(config.DB_FILE)
    conn.executescript(_SCHEMA)
    return conn


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

    with _connect() as conn:
        for c in courses:
            for t in getattr(c, "tasks", []) or []:
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
                            overdue=excluded.overdue
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
                    log.debug("写入成绩历史失败（%s）：%s", t.title, e)

    if rows:
        log.info("成绩历史：写入/更新 %d 条", rows)
    return rows


def record_snapshot(overall: float | None, payload: dict) -> None:
    with _connect() as conn:
        conn.execute("INSERT INTO snapshots (taken_at, overall_gpa, payload) VALUES (?,?,?)",
            (datetime.now().isoformat(timespec="seconds"), overall,
             json.dumps(payload, ensure_ascii=False)),
        )


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
