"""搜索结果（0.5.2 新增）。

为什么要「强制刷新成绩」
------------------------
老师登了分之后还会改分，而界面上的成绩来自上一次抓取的缓存。
搜索是「我现在想确认这条作业到底得了多少」的动作 ——
如果拿缓存回答，就可能给出一个已经被改掉的旧分数。

所以搜索本身带一次按需重抓：
  · 指定了科目 → 只重抓那一门（约 2–3 秒，不打扰其他课程）
  · 没指定科目 → 重抓全部（约 10–15 秒，有进度提示）
重抓走 pipeline 里同一套逻辑（含重试与限速退避），不是另写一条取数路径 ——
两条路径迟早会漂移。

匹配规则
--------
  · **不区分大小写**
  · 多个关键词用空格分开 → 全部命中才算（AND），顺序无关
  · 匹配范围：标题、课程名、分类、类型、成绩原文、等级、提交状态
    （**不搜老师要求正文** —— 那里命中多半是无关的长文本，反而盖住标题命中）
  · 排序：整个关键词都在标题里 > 有关键词在标题里 > 只在其他字段命中，
    同档按课程名、截止日期排
"""
from __future__ import annotations

import logging
import time

from . import pipeline

log = logging.getLogger(__name__)

#  参与匹配的字段。标题单独拎出来用于排序，其余合成一个干草堆。
_TITLE = "title"
_REST = ("course", "category", "kind", "score", "level", "status")


def _txt(v) -> str:
    return "" if v is None else str(v)


def _norm(v) -> str:
    """归一化：转字符串 + 去首尾空白 + 转小写（用 casefold 处理非 ASCII）。"""
    return _txt(v).strip().casefold()


def _hay(t: dict) -> tuple[str, str]:
    """返回（标题的小写形式, 其余字段拼成的小写串）。"""
    title = _norm(t.get(_TITLE))
    rest = " ".join(_norm(t.get(k)) for k in _REST)
    return title, rest


def _terms(query: str) -> list[str]:
    """把查询串拆成关键词（按空白），全部小写。"""
    return [w for w in _norm(query).split() if w]


def _match(t: dict, terms: list[str]) -> int:
    """命中返回排序档位（0 最好），不命中返回 -1。

    档位：
      0 —— 所有关键词都出现在标题里
      1 —— 至少一个关键词出现在标题里
      2 —— 只在课程 / 分类 / 类型 / 成绩等字段里命中
    """
    if not terms:
        return 2
    title, rest = _hay(t)
    whole = (title + " " + rest).strip()
    for w in terms:
        if w not in whole:
            return -1
    in_title = sum(1 for w in terms if w in title)
    if in_title == len(terms):
        return 0
    if in_title > 0:
        return 1
    return 2


def _due_key(t: dict) -> tuple[int, str]:
    """按截止日期排（没有日期的排后面）。"""
    d = _txt(t.get("due_date")).strip()
    return (1 if not d else 0, d)


def run(query: str = "", course: str = "", force: bool = True) -> dict:
    """执行一次搜索。返回给前端的结构见文件末尾的说明。"""
    t0 = time.time()
    terms = _terms(query)
    want_course = _txt(course).strip()

    # ── ① 按需强制重抓 ──
    refresh = {"ok": True, "courses": [], "changed": [], "took_ms": 0,
               "skipped": True, "error": ""}
    if force:
        try:
            names = [want_course] if want_course else None
            refresh = pipeline.refresh_courses_only(names)
            refresh["skipped"] = False
        except Exception as e:                     # 刷新失败不影响搜索本身
            log.warning("搜索前刷新成绩失败：%s", e)
            refresh = {"ok": False, "courses": [], "changed": [],
                       "took_ms": 0, "skipped": False, "error": str(e)}

    # ── ② 从当前内存数据里筛 ──
    snap = pipeline.snapshot()
    all_tasks = [t for t in (snap.get("tasks") or []) if isinstance(t, dict)]

    hits: list[tuple[int, dict]] = []
    for t in all_tasks:
        if want_course and _norm(t.get("course")) != _norm(want_course):
            continue
        rank = _match(t, terms)
        if rank < 0:
            continue
        hits.append((rank, t))

    hits.sort(key=lambda rk: (rk[0],
                              _norm(rk[1].get("course")),
                              _due_key(rk[1]),
                              _norm(rk[1].get("title"))))

    # ── ③ 按课程分组（组的顺序跟随排序结果）──
    groups: list[dict] = []
    index: dict[str, dict] = {}
    for _rank, t in hits:
        key = _txt(t.get("course"))
        g = index.get(key)
        if g is None:
            g = {
                "course": key,
                "course_url": _txt(t.get("course_url")),
                "class_id": _txt(t.get("class_id")),
                "tasks": [],
            }
            index[key] = g
            groups.append(g)
        g["tasks"].append(t)

    for g in groups:
        g["count"] = len(g["tasks"])

    took = int((time.time() - t0) * 1000)
    log.info("搜索「%s」%s：%d 条命中 / %d 门课程，刷新 %s（%d 毫秒）",
             query or "（空）",
             f"·科目 {want_course}" if want_course else "",
             len(hits), len(groups),
             "已执行" if force else "跳过", took)

    return {
        "ok": True,
        "query": _txt(query),
        "course": want_course,
        "force": bool(force),
        "terms": terms,
        "total": len(hits),
        "groups": groups,
        "courses_total": len(groups),
        "refresh": refresh,
        "took_ms": took,
        "error": "",
    }
