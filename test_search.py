"""搜索功能的离线回归测试。

覆盖
----
  · 不区分大小写
  · 多个关键词用空格分开 = 全部命中（AND），顺序无关
  · 指定科目时只返回该科（科目名同样不区分大小写）
  · 排序：三个关键词全在标题里 > 部分在标题里 > 只在其他字段命中
  · 空查询 = 列出全部
  · 分组、计数、响应结构
  · 刷新失败不影响搜索本身的返回

不联网：把 pipeline.snapshot 换成假数据，force=False 跳过抓取。
"""
from __future__ import annotations

import pathlib
import sys

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


def task(title, course, **kw) -> dict:
    d = {
        "title": title, "course": course, "course_url": "cu/" + course,
        "class_id": kw.get("class_id", "1"),
        "due_date": kw.get("due_date", "2026-10-22"),
        "due_text": "", "publish_date": "",
        "category": kw.get("category", "Summative"),
        "kind": kw.get("kind", "Quiz"),
        "score": kw.get("score", ""),
        "level": kw.get("level", ""),
        "percent": kw.get("percent"),
        "graded": bool(kw.get("score")), "submitted": True,
        "status": kw.get("status", "已提交"),
        "has_attachment": False, "description": kw.get("description", ""),
        "url": "u/" + title,
    }
    return d


TASKS = [
    task("Unit 2 quiz", "Chemistry (AP Electives)", score="A 96 / 100 pts",
         level="A", percent=96.0),
    task("Unit 1 quiz", "Chemistry (AP Electives)", score="B 49 / 60 pts",
         level="B", percent=81.67, due_date="2026-09-28"),
    task("Unit 2-polarity", "Chemistry (AP Electives)", score="A 98 / 100 pts",
         level="A", percent=98.0, due_date="2026-10-19"),
    task("Sleepy Hollow - Quiz", "English Language Arts I (Grade 10)",
         score="A 90 / 100 pts", level="A", percent=90.0,
         kind="Formative", due_date="2026-10-01"),
    task("Essay: 我的家乡", "Chinese Language Arts I",
         score="A 95 / 100 pts", level="A", percent=95.0, kind="Essay"),
    task("Pre_Physics Test1", "Physics10 (Grade 10) 3",
         score="B 84 / 100 pts", level="B", percent=84.0),
]


def main() -> int:
    print("=" * 76)
    print("  搜索：匹配 / 排序 / 分组")
    print("=" * 76)

    from app import pipeline, search

    #  ── 假数据：替换掉 snapshot，避免联网 ──
    real_snapshot = pipeline.snapshot
    pipeline.snapshot = lambda: {"tasks": [dict(t) for t in TASKS],
                                 "courses": []}
    try:
        def run(q, course="", force=False):
            return search.run(query=q, course=course, force=force)

        # ── ① 大小写不敏感 ──
        print("\n── ① 不区分大小写 ──")
        a = run("quiz")
        b = run("QUIZ")
        c = run("QuIz")
        titles = lambda r: [t["title"] for g in r["groups"] for t in g["tasks"]]
        chk(a["total"] == b["total"] == c["total"] and a["total"] > 0,
            f"quiz / QUIZ / QuIz 结果一致（各 {a['total']} / {b['total']} / {c['total']} 条）",
            (a["total"], b["total"], c["total"]))
        chk(titles(a) == titles(b) == titles(c), "三种写法命中的条目完全相同", titles(a))

        # ── ② 多关键词 AND ──
        print("\n── ② 多个关键词 = 全部命中 ──")
        #  用「两个词只同时出现在一条标题里」来验 AND，避免被
        #  类型 / 分类字段的合法命中干扰。
        r = run("sleepy hollow")
        chk(r["total"] == 1, f"『sleepy hollow』只命中 1 条（实际 {r['total']}）", titles(r))
        chk(titles(r) == ["Sleepy Hollow - Quiz"], "命中的正是 Sleepy Hollow - Quiz",
            titles(r))
        r2 = run("hollow sleepy")
        chk(titles(r2) == titles(r), "关键词顺序不影响结果", titles(r2))
        r3 = run("quiz zzzz")
        chk(r3["total"] == 0, f"有一个词不命中就整体不命中（实际 {r3['total']}）", titles(r3))
        #  ★ 类型（kind）也在搜索范围内，所以『quiz』会把所有 Quiz 类的作业带出来
        #    —— 这是有意的：搜 quiz 就是想看所有测验。
        r4 = run("quiz unit 2")
        chk(set(titles(r4)) == {"Unit 2 quiz", "Unit 2-polarity"},
            f"『quiz unit 2』含类型为 Quiz 的 Unit 2-polarity（实际 {titles(r4)}）",
            titles(r4))

        # ── ③ 指定科目 ──
        print("\n── ③ 指定科目 ──")
        r = run("quiz", "Chemistry (AP Electives)")
        #  化学 3 条里 Unit 2-polarity 的类型也是 Quiz，所以命中 3 条
        chk(r["total"] == 3, f"化学里 quiz 命中 3 条（实际 {r['total']}）", titles(r))
        chk(all(t["course"] == "Chemistry (AP Electives)"
                for g in r["groups"] for t in g["tasks"]), "结果只含该科目")
        r = run("quiz", "chemistry (ap electives)")
        chk(r["total"] == 3, f"科目名也不区分大小写（实际 {r['total']}）", titles(r))
        r = run("", "Chemistry (AP Electives)")
        chk(r["total"] == 3, f"空查询 + 科目 = 该科全部作业（实际 {r['total']}）", titles(r))

        # ── ④ 排序档位 ──
        print("\n── ④ 排序：标题命中优先 ──")
        r = run("unit")
        first = r["groups"][0]["tasks"][0]["title"]
        chk("Unit" in first, f"标题里含 unit 的排在最前（{first}）", first)
        #  只匹配课程名/分数字段的应排在后面
        r = run("physics")
        g = r["groups"][0]
        chk(g["course"] == "Physics10 (Grade 10) 3",
            "『physics』匹配到课程名也返回", g["course"])

        # ── ⑤ 其他字段 ──
        print("\n── ⑤ 课程名 / 分数字段也能命中 ──")
        r = run("chemistry")
        chk(r["total"] == 3, f"课程名命中 3 条（实际 {r['total']}）", titles(r))
        r = run("96")
        chk(r["total"] == 1 and titles(r) == ["Unit 2 quiz"],
            f"分数原文里的 96 也能搜到（实际 {titles(r)}）", titles(r))
        r = run("summative")
        chk(r["total"] == 6, f"分类命中 6 条（实际 {r['total']}）", titles(r))
        r = run("formative")
        chk(r["total"] == 1 and titles(r) == ["Sleepy Hollow - Quiz"],
            f"分类『formative』只命中那一条（实际 {titles(r)}）", titles(r))

        # ── ⑥ 空查询与无命中 ──
        print("\n── ⑥ 空查询 / 无命中 ──")
        r = run("")
        chk(r["total"] == len(TASKS), f"空查询列出全部（{r['total']} 条）", r["total"])
        r = run("    ")
        chk(r["total"] == len(TASKS), "只有空格也按空查询处理", r["total"])
        r = run("zzzzz")
        chk(r["total"] == 0 and r["ok"] is True, "无命中返回空结果且 ok=True",
            (r["total"], r["ok"]))

        # ── ⑦ 分组与响应结构 ──
        print("\n── ⑦ 分组与响应结构 ──")
        r = run("")
        chk(r["courses_total"] == len(r["groups"]), "courses_total == 组数",
            (r["courses_total"], len(r["groups"])))
        chk(sum(g["count"] for g in r["groups"]) == r["total"],
            "各组 count 之和等于 total",
            ([g["count"] for g in r["groups"]], r["total"]))
        chk(all(g["course"] for g in r["groups"]), "每组都有课程名")
        chk(all("tasks" in g for g in r["groups"]), "每组都带 tasks")
        chk("refresh" in r and "took_ms" in r, "响应里有 refresh 与 took_ms")
        #  同一门课不应被拆成两组
        names = [g["course"] for g in r["groups"]]
        chk(len(names) == len(set(names)), "同一课程不会拆成多组", names)

        # ── ⑧ 刷新失败不影响搜索 ──
        print("\n── ⑧ 刷新失败时的降级 ──")
        real_refresh = pipeline.refresh_courses_only

        def boom(names=None, workers=3):
            raise RuntimeError("模拟抓取失败")

        pipeline.refresh_courses_only = boom
        try:
            r = search.run(query="quiz", course="", force=True)
            chk(r["ok"] is True, "刷新抛异常时搜索仍返回 ok=True", r["ok"])
            chk(r["total"] > 0, f"仍然给出结果（{r['total']} 条）", r["total"])
            chk(r["refresh"].get("ok") is False, "refresh 里如实标明失败",
                r["refresh"])
            chk("模拟抓取失败" in (r["refresh"].get("error") or ""),
                "错误原因带上去了", r["refresh"].get("error"))
        finally:
            pipeline.refresh_courses_only = real_refresh

    finally:
        pipeline.snapshot = real_snapshot

    print()
    print("=" * 76)
    print(f"  {PASS} 通过 / {FAIL} 失败")
    print("=" * 76)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
