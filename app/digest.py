"""晚报 —— 每个工作日晚 9 点自动生成本日总结，在组件顶部点开查看。

【用户要求】
    每工作日晚 9 点生成晚报，在组件顶部点开，包括：
      · 简单平均的总 GPA
      · 新增作业
      · 新增成绩
      · 等等

【实现思路】
  1. 每次刷新抓取后，把「本次快照」与「上一次快照」做 diff
     → 得到新增任务 / 新增成绩 / 完成情况
  2. 每个**工作日**（周一~周五）晚 21:00 后，第一次打开组件时
     自动生成当日晚报（含累计变动）
  3. 晚报存在 `data/digests.json`（只留最近 30 天）
  4. 顶部出现一颗「小圆点」提示有新晚报，点开看详情

【为什么不做成后台定时任务】
  组件不是常驻进程。用一个「按日期归档」的纯函数更稳：
    - 任何时候打开都能补齐（哪怕电脑关了一整天）
    - 不依赖后台进程活着
    - 不会因为错过时间点而丢数据
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime, timedelta

from . import config

try:  # 兼容两种入口（app.digest / digest）
    from .gpa import LETTER_TO_GPA, percent_to_letter
except Exception:  # pragma: no cover
    LETTER_TO_GPA = {}
    percent_to_letter = lambda p: ""  # noqa: E731

log = logging.getLogger("digest")

DIGEST_FILE = config.DATA_DIR / "digests.json"
SNAPSHOT_FILE = config.DATA_DIR / "digest_snapshot.json"
STATE_FILE = config.DATA_DIR / "digest_state.json"

# 晚报生成时间（本地时间，24 小时制）
DIGEST_HOUR = 21

# 只在工作日生成（周一=0 … 周日=6）
WEEKDAYS = {0, 1, 2, 3, 4}

# 保留最近多少天的晚报
KEEP_DAYS = 30

_lock = threading.RLock()

WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


# ---------------------------------------------------------------- 工具

def _read_json(path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("读取 %s 失败：%s", path.name, e)
    return default


def _write_json(path, data) -> None:
    """原子写（先写临时文件再替换），避免半截文件。"""
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(path)
    except Exception as e:
        log.warning("写入 %s 失败：%s", path.name, e)


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def today_str() -> str:
    return date.today().isoformat()


def is_workday(d: date | None = None) -> bool:
    return (d or date.today()).weekday() in WEEKDAYS


def gpa_of(percent: float | None) -> float | None:
    """百分制 → 4.0 GPA（与 gpa.py 的换算表一致）。"""
    if percent is None:
        return None
    if percent >= 93:
        return 4.0
    if percent >= 90:
        return 3.7
    if percent >= 87:
        return 3.3
    if percent >= 83:
        return 3.0
    if percent >= 80:
        return 2.7
    if percent >= 77:
        return 2.3
    if percent >= 73:
        return 2.0
    if percent >= 70:
        return 1.7
    if percent >= 67:
        return 1.3
    if percent >= 63:
        return 1.0
    if percent >= 60:
        return 0.7
    return 0.0


def letter_of(percent: float | None) -> str:
    if percent is None:
        return "—"
    try:
        return percent_to_letter(percent)
    except Exception:
        return ""


# ---------------------------------------------------------------- 快照

def _task_key(t: dict) -> str:
    """任务的稳定标识（用于 diff 新增/变化）。"""
    tid = ""
    url = t.get("url") or ""
    if "/core_tasks/" in url:
        tid = url.rsplit("/core_tasks/", 1)[-1].split("/")[0]
    return f"tid:{tid}" if tid else f"t:{t.get('course','')}|{t.get('title','')}"


def _make_snapshot(data: dict) -> dict:
    """把当前数据压成「便于 diff」的轻量结构。"""
    tasks = {}
    for t in (data.get("tasks") or []):
        k = _task_key(t)
        tasks[k] = {
            "title": t.get("title", ""),
            "course": t.get("course", ""),
            "url": t.get("url", ""),
            "due_date": t.get("due_date", ""),
            "kind": t.get("kind", ""),
            "category": t.get("category", ""),
            "score": t.get("score", ""),
            "level": t.get("level", ""),
            "percent": t.get("percent"),
            "graded": bool(t.get("graded")),
            "submitted": bool(t.get("submitted")),
            "completed": bool(t.get("completed")),
            "late": bool(t.get("late")),
            "has_attachment": bool(t.get("has_attachment")),
        }

    courses = {}
    for c in (data.get("courses") or []):
        courses[c.get("class_id", "")] = {
            "name": c.get("name", ""),
            "percent": c.get("overall_percent"),
            "level": c.get("overall_level", ""),
            "pending": c.get("pending_count", 0),
            "late": c.get("late_count", 0),
        }

    return {
        "at": now_str(),
        "tasks": tasks,
        "courses": courses,
        "status": data.get("status", ""),
    }


# ---------------------------------------------------------------- 计算

def _overall_gpa(courses: dict) -> dict:
    """简单平均的总 GPA（用户要求：简单平均，不做加权）。"""
    pcts = [c["percent"] for c in courses.values()
            if c.get("percent") is not None]
    if not pcts:
        return {"gpa": None, "percent": None, "letter": "—", "count": 0}

    mean = sum(pcts) / len(pcts)
    gpas = [gpa_of(p) for p in pcts]
    gpas = [g for g in gpas if g is not None]
    return {
        "gpa": round(sum(gpas) / len(gpas), 2) if gpas else None,
        "percent": round(mean, 2),
        "letter": letter_of(mean),
        "count": len(pcts),
    }


def _diff(prev: dict | None, cur: dict) -> dict:
    """比较两次快照，得出「新增 / 新成绩 / 新完成」。"""
    if not prev:
        return {
            "new_tasks": [], "new_grades": [], "new_done": [],
            "due_soon": [], "overdue": [],
            "first_run": True,
        }

    p_tasks = prev.get("tasks") or {}
    c_tasks = cur.get("tasks") or {}

    new_tasks, new_grades, new_done = [], [], []

    for k, t in c_tasks.items():
        old = p_tasks.get(k)

        # ① 新增任务（上次快照里没有）
        if old is None:
            new_tasks.append(t)
            continue

        # ② 新增成绩（这次有分数/等级，上次没有）
        had = bool(old.get("graded")) or bool(old.get("score"))
        has = bool(t.get("graded")) or bool(t.get("score"))
        if has and not had:
            new_grades.append(t)

        # ③ 新完成（上次未完成，这次完成）
        if t.get("completed") and not old.get("completed"):
            new_done.append(t)

    # ④ 今天到期 / 已逾期（无论是否新增，晚报都该提醒）
    today = date.today().isoformat()
    due_soon, overdue = [], []
    for t in c_tasks.values():
        if t.get("completed"):
            continue
        d = (t.get("due_date") or "")[:10]
        if not d:
            continue
        if d == today:
            due_soon.append(t)
        elif d < today:
            overdue.append(t)

    return {
        "new_tasks": new_tasks,
        "new_grades": new_grades,
        "new_done": new_done,
        "due_soon": due_soon,
        "overdue": overdue,
        "first_run": False,
    }


def build(data: dict) -> dict:
    """根据当前抓取数据生成一份「今日晚报」（不落盘）。"""
    with _lock:
        prev = _read_json(SNAPSHOT_FILE, None)
        cur = _make_snapshot(data)
        changes = _diff(prev, cur)
        gpa = _overall_gpa(cur["courses"])

        # 上次的 GPA（用于显示变化趋势）
        prev_gpa = None
        if prev:
            prev_gpa = _overall_gpa(prev.get("courses") or {})

        delta = None
        if gpa["percent"] is not None and prev_gpa and prev_gpa["percent"] is not None:
            delta = round(gpa["percent"] - prev_gpa["percent"], 2)

        d = date.today()
        return {
            "ok": True,
            "date": d.isoformat(),
            "weekday": WEEKDAY_CN[d.weekday()],
            "generated": now_str(),
            "first_run": changes["first_run"],
            "gpa": gpa,
            "gpa_delta": delta,
            "new_tasks": changes["new_tasks"][:40],
            "new_grades": changes["new_grades"][:40],
            "new_done": changes["new_done"][:40],
            "due_soon": changes["due_soon"][:40],
            "overdue": changes["overdue"][:40],
            "counts": {
                "new_tasks": len(changes["new_tasks"]),
                "new_grades": len(changes["new_grades"]),
                "new_done": len(changes["new_done"]),
                "due_soon": len(changes["due_soon"]),
                "overdue": len(changes["overdue"]),
                "pending": sum(1 for t in cur["tasks"].values() if not t.get("completed")
                ),
                "courses": len(cur["courses"]),
            },
        }


# ---------------------------------------------------------------- 落盘

def save_snapshot(data: dict) -> None:
    """把当前数据存为「上次快照」（供下次 diff）。"""
    with _lock:
        _write_json(SNAPSHOT_FILE, _make_snapshot(data))


def _load_all() -> list[dict]:
    items = _read_json(DIGEST_FILE, [])
    return items if isinstance(items, list) else []


def _save_all(items: list[dict]) -> None:
    """保存并裁掉超过 KEEP_DAYS 天的旧晚报。"""
    cutoff = (date.today() - timedelta(days=KEEP_DAYS)).isoformat()
    items = [x for x in items if (x.get("date") or "") >= cutoff]
    items.sort(key=lambda x: x.get("date") or "", reverse=True)
    _write_json(DIGEST_FILE, items[:KEEP_DAYS])


def ensure_today(data: dict) -> dict | None:
    """确保「今天」的晚报已生成（若已过 21:00 且是工作日）。

    返回：本次新生成的晚报（若是刚生成）；否则 None。

     设计要点：
      - 只在工作日生成
      - 未到 21:00 不生成（避免白天刷出一堆半成品晚报）
      - 已生成过就不重复生成（同一天只生成一次）
      - 电脑整天没开 → 第二天打开时，只补「当前这一天」，不补历史
        （历史数据无法重建，硬补只会产生空晚报）
    """
    with _lock:
        d = date.today()
        if not is_workday(d):
            return None
        if datetime.now().hour < DIGEST_HOUR:
            return None

        items = _load_all()
        if any(x.get("date") == d.isoformat() for x in items):
            return None                       # 今天已经生成过

        digest = build(data)
        items.append(digest)
        _save_all(items)
        _write_json(STATE_FILE, {
            "last_date": d.isoformat(),
            "last_at": now_str(),
        })
        log.info("已生成 %s 晚报（新增任务 %s / 新成绩 %s）",
                 d.isoformat(),
                 digest["counts"]["new_tasks"],
                 digest["counts"]["new_grades"])
        return digest


def latest() -> dict | None:
    """最近一份晚报。"""
    with _lock:
        items = _load_all()
        return items[0] if items else None


def list_all(limit: int = 30) -> list[dict]:
    with _lock:
        return _load_all()[:limit]


def has_unread() -> bool:
    """今天有晚报，且用户还没看过 → 顶部显示小红点。"""
    with _lock:
        d = today_str()
        items = _load_all()
        if not any(x.get("date") == d for x in items):
            return False
        st = _read_json(STATE_FILE, {})
        return st.get("read_date") != d


def mark_read() -> None:
    with _lock:
        st = _read_json(STATE_FILE, {})
        st["read_date"] = today_str()
        st["read_at"] = now_str()
        _write_json(STATE_FILE, st)


def status() -> dict:
    """给 UI 的概要状态。"""
    with _lock:
        items = _load_all()
        return {
            "ok": True,
            "unread": has_unread(),
            "has_any": bool(items),
            "date": items[0].get("date") if items else None,
            "count": len(items),
            "hour": DIGEST_HOUR,
            "workday": is_workday(),
        }
