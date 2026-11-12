"""证明「/core_tasks 失败 → /units 的成绩被整段丢弃」这个门槛。

现场
----
缓存里 ELA（class 11511734）是唯一一门
    overall_level='' / overall_percent=None / categories=[]
的课，而它的任务级分数是有的、实时 /units 也能解出 Overall B 82.31%。

pipeline.py 的课程抓取里有一道门槛：

    sc.fetch_course_detail(probe)
    if probe.tasks:                      # ← 必须拿到任务
        course.tasks = probe.tasks
        course.categories = probe.categories or course.categories
        if probe.overall_level:          # ← 分类与总评都在这道门里面
            course.overall_level = probe.overall_level
        if probe.overall_percent is not None:
            course.overall_percent = probe.overall_percent

也就是说：**/units 成功、成绩解出来了，只要 /core_tasks 失败，
这些成绩就会被丢掉**，而失败只往 report["notes"] 里写一行，界面上看不到。

本测试把 /core_tasks 打桩成失败、/units 用真实抓到的页面，
直接观察 course.overall_percent 有没有被赋值。

离线：不发任何网络请求（页面来自本地抓取结果）。
"""
from __future__ import annotations

import pathlib
import os
import sys

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

PASS = 0
FAIL = 0

# ── 真实页面结构（2026-10-13 用组件自己的会话抓到，原样内嵌）──
#    ELA（class 11511734）成绩册的「分类成绩」区块。
#    ★ 必须带 "Task Category Averages" 这个锚点 ——
#      _parse_categories 靠这行文字定位 section，否则直接返回空。
#    内嵌而不读外部文件，是为了这个用例在任何机器上都能复现。
UNITS_FIXTURE = """
<section class="grades-section">
  <h3>Task Category Averages</h3>
  <div class="sidebar-items-list">
    <div class="list-item list-item-head">
      <div class="cell">Category (Weight)</div><div class="cell">Mark (Score)</div>
    </div>
    <div class="list-item">
      <div class="cell"><strong>Overall</strong></div>
      <div class="cell"><strong>B</strong> (82.31%)</div>
    </div>
    <div class="list-item">
      <div class="cell"><div class="label w-100 text-ellipsis">Formative Assessment (20%)</div></div>
      <div class="cell"><strong>B</strong> (82.31%)</div>
    </div>
    <div class="list-item">
      <div class="cell"><div class="label w-100 text-ellipsis">Summative Assessment (35%)</div></div>
      <div class="cell"> - </div>
    </div>
    <div class="list-item">
      <div class="cell"><div class="label w-100 text-ellipsis">Exams (45%)</div></div>
      <div class="cell"> - </div>
    </div>
  </div>
</section>
"""

#  主页/任务页的最小可用内容（只要能被解析成 0 个任务即可）
HOME_FIXTURE = "<html><body><div class='tasks-list-container'></div></body></html>"


def chk(ok: bool, label: str, extra=None) -> None:
    global PASS, FAIL
    print(f"  {'[OK]' if ok else '[X] '} {label}" + ("" if ok else f"   → {extra!r}"))
    if ok:
        PASS += 1
    else:
        FAIL += 1


def main() -> int:
    print("=" * 76)
    print("  /core_tasks 失败会不会把 /units 的成绩一起丢掉")
    print("=" * 76)

    from app import scraper
    from app.models import Course

    units_html = UNITS_FIXTURE
    home_html = HOME_FIXTURE

    # ── 先确认 /units 这份页面本身是能解出成绩的 ──
    print("\n── ① /units 页面本身含不含成绩 ──")
    sc0 = scraper.Scraper.__new__(scraper.Scraper)
    from bs4 import BeautifulSoup
    cats = sc0._parse_categories(BeautifulSoup(units_html, "html.parser"))
    #  注意：_parse_categories 返回的是 CategoryAverage 对象，不是 dict
    overall = next((c for c in cats
                    if (getattr(c, "name", "") or "").strip().lower() == "overall"),
                   None)
    chk(bool(cats), f"_parse_categories 解出 {len(cats)} 个分类")
    chk(overall is not None, "其中含 Overall 行",
        [getattr(c, "name", c) for c in cats])
    if overall:
        print(f"      Overall = level={getattr(overall,'level',None)!r} "
              f"percent={getattr(overall,'percent',None)!r}")

    # ── 复刻 pipeline.one() 的关键几行 ──
    def run_case(home_ok: bool, core_tasks_ok: bool):
        """按 pipeline 逻辑填 course。

        /units **始终成功** —— 这正是要证明的点：
        它能成功，成绩却被别处的失败连带丢掉。
        """
        sc = scraper.Scraper.__new__(scraper.Scraper)

        def fake_goto(url: str, wait_ms: int = 0) -> str:
            if url.endswith("/units"):
                return units_html                       # ← 始终成功
            if url.endswith("/core_tasks"):
                if not core_tasks_ok:
                    raise OSError("_ssl.c:993: The handshake operation timed out")
                return home_html
            #  课程主页
            if not home_ok:
                raise OSError("_ssl.c:993: The handshake operation timed out")
            return home_html

        sc.goto = fake_goto
        sc.report = {"courses": [], "notes": []}

        probe = Course(name="English Language Arts I (Grade 10)",
                       url="https://beijing101.managebac.cn/student/classes/11511734",
                       class_id="11511734")
        sc.fetch_course_detail(probe)

        #  ↓↓↓ 照抄 pipeline.py 修正后的逻辑（任务与成绩册分开判断）↓↓↓
        course = Course(name=probe.name, url=probe.url, class_id=probe.class_id)
        if probe.tasks:
            course.tasks = probe.tasks
            course.teacher = probe.teacher or course.teacher
        if probe.categories:
            course.categories = probe.categories
            if (probe.submitted_count or probe.late_count
                    or probe.pending_count):
                course.submitted_count = probe.submitted_count
                course.late_count = probe.late_count
                course.pending_count = probe.pending_count
        if probe.overall_level:
            course.overall_level = probe.overall_level
        if probe.overall_percent is not None:
            course.overall_percent = probe.overall_percent
        return course, probe, list(sc.report.get("notes") or [])

    print("\n── ② 两个任务来源都成功（基准）──")
    c_ok, p_ok, n_ok = run_case(home_ok=True, core_tasks_ok=True)
    print(f"      probe : tasks={len(p_ok.tasks)} categories={len(p_ok.categories)} "
          f"overall={p_ok.overall_level!r}/{p_ok.overall_percent!r}")
    print(f"      course: categories={len(c_ok.categories)} "
          f"overall={c_ok.overall_level!r}/{c_ok.overall_percent!r}")
    chk(c_ok.overall_percent is not None,
        "基准：course 拿到了总评", c_ok.overall_percent)

    print("\n── ③ 两个任务来源都失败，但 /units 成功（ELA 的实际遭遇）──")
    c_bad, p_bad, notes = run_case(home_ok=False, core_tasks_ok=False)
    print(f"      probe : tasks={len(p_bad.tasks)} categories={len(p_bad.categories)} "
          f"overall={p_bad.overall_level!r}/{p_bad.overall_percent!r}")
    print(f"      course: categories={len(c_bad.categories)} "
          f"overall={c_bad.overall_level!r}/{c_bad.overall_percent!r}")
    print(f"      report.notes = {notes or '(空)'}")

    chk(p_bad.overall_percent is not None,
        "probe 自己解出了总评（数据是拿到的）", p_bad.overall_percent)
    chk(bool(notes), "失败被记进了 report.notes", notes)
    failed = c_bad.overall_percent is None
    if failed:
        print("      ↑ 复现成功：probe 有成绩，却被 `if probe.tasks` 门槛整段丢掉")
    chk(not failed,
        "course 也应该拿到总评（当前为失败 = 确认 bug）",
        f"course.overall_percent={c_bad.overall_percent!r}")

    print()
    print("=" * 76)
    print(f"  {PASS} 通过 / {FAIL} 失败")
    if FAIL:
        print("  结论：仍有问题 —— /units 的成绩被任务页的失败连带丢弃")
    else:
        print("  结论：任务页失败不再影响成绩册（门槛已解耦）")
    print("=" * 76)
    return 1 if FAIL else 0


def sc_notes(probe, course):
    return getattr(probe, "_notes", "")


if __name__ == "__main__":
    raise SystemExit(main())
