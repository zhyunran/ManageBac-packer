"""成绩册「在哪个页面」的回归测试（离线，内嵌真实结构）。

背景（2026-10-13 实测）
----------------------
成绩册（Task Category Averages：Overall / 各分类权重与得分）原本在课程的
``/units`` 页面。2026-10-13 实测发现 ManageBac 把它**搬到了 ``/core_tasks``**：

    /core_tasks : Task Category Averages 存在，解出 4 个分类 → Overall B 82.31%
    /units      : 一个分类都解不出来（0 个），页面上只剩单元列表

后果是整个 GPA 没有数据：所有课的总评同时变空，``graded_count`` 变 0，
用户看到的就是「GPA 计算系统炸了」。

修复思路：**两个页面都试着解析**，谁有就用谁。这样 ManageBac 再挪一次
也不会全灭，而且 /core_tasks 本来就要抓（取任务），不额外增加请求。

本测试钉住三件事：
  1. ``_apply_gradebook`` 能从成绩册所在的页面解出分类、总评与完成统计
  2. 成绩册**不在**的页面要如实返回 False（不能假装成功、也不能把
     已有数据清空）
  3. 两页都试的顺序：先 core_tasks，没有再 units
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


# ── 成绩册结构（2026-10-13 从真实 /core_tasks 页面原样抄下来的）──
GRADEBOOK_SECTION = """
<section>
  <h6 aria-level="3" class="text-secondary mb-3 d-flex align-items-center">
    Task Category Averages
  </h6>
  <div class="sidebar-items-list">
    <div class="list-item list-item-head">
      <div class="cell">Category (Weight)</div>
      <div class="cell">Mark (Score)</div>
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
  <p>View the average grades per task category calculated in the class
     for the academic term.</p>
</section>
"""

#  完成统计的真实结构（2026-10-13 从 /core_tasks 页面抄下来）
#  ★ 必须带 "Overall Task Completion" 这个锚点 ——
#    _parse_completion 靠这行文字定位容器，否则一个数都读不到。
COMPLETION = """
<div class="task-completion-chart-section">
  <div class="task-completion-chart-section-title flex">
    <h3>Overall Task Completion</h3>
    <div class="btn-group">
      <a class="btn btn-secondary btn-icon active"
         data-bs-title="Overall Task Completion"
         href="/student/classes/11511734/charts/task_completion?term_id=109610">…</a>
    </div>
  </div>
  <div class="task-completion-chart-body">
    <span>- 16 Submitted - 0 Late - 0 Pending - 0</span>
  </div>
</div>
"""

#  新版 /units：只剩单元列表，没有成绩册
UNITS_WITHOUT_GRADEBOOK = """
<html><body>
  <a href="/student/classes/11511734/units/830673/presentations">Unit 1 - Reflection</a>
  <a href="/student/classes/11511734/units/869044/presentations">Unit 1- Grammar</a>
  <div class="label">Formative</div><div class="label">Homework</div>
</body></html>
"""

PAGE_WITH_GRADEBOOK = f"<html><body>{GRADEBOOK_SECTION}{COMPLETION}</body></html>"


def main() -> int:
    print("=" * 76)
    print("  成绩册在哪个页面（ManageBac 改版回归）")
    print("=" * 76)

    from bs4 import BeautifulSoup
    from app import scraper
    from app.models import Course

    sc = scraper.Scraper.__new__(scraper.Scraper)

    # ── ① 有成绩册的页面：要解出分类、总评、完成统计 ──
    print("\n── ① 成绩册所在的页面 ──")
    c1 = Course(name="ELA", url="", class_id="11511734")
    ok = sc._apply_gradebook(
        BeautifulSoup(PAGE_WITH_GRADEBOOK, "html.parser"), c1)
    chk(ok is True, "_apply_gradebook 返回 True", ok)
    chk(len(c1.categories) == 4, f"解出 4 个分类（实际 {len(c1.categories)}）",
        [getattr(c, "name", c) for c in c1.categories])
    chk(c1.overall_level == "B", f"总评等级 = B（实际 {c1.overall_level!r}）",
        c1.overall_level)
    chk(c1.overall_percent == 82.31,
        f"总评百分比 = 82.31（实际 {c1.overall_percent!r}）", c1.overall_percent)
    chk(c1.submitted_count == 16,
        f"完成统计 submitted = 16（实际 {c1.submitted_count}）",
        c1.submitted_count)

    # ── ② 没有成绩册的页面（新版 /units）：如实返回 False ──
    print("\n── ② 没有成绩册的页面（新版 /units）──")
    c2 = Course(name="ELA", url="", class_id="11511734")
    #  先给它塞一份旧数据，验证「解不出来时不会把已有数据清空」
    c2.categories = list(c1.categories)
    c2.overall_level = "B"
    c2.overall_percent = 82.31
    ok2 = sc._apply_gradebook(
        BeautifulSoup(UNITS_WITHOUT_GRADEBOOK, "html.parser"), c2)
    chk(ok2 is False, "如实返回 False（不能假装成功）", ok2)
    chk(len(c2.categories) == 4 and c2.overall_percent == 82.31,
        "已有成绩没被清空（沿用旧值）",
        (len(c2.categories), c2.overall_percent))

    # ── ③ 完成统计的解析（只在有成绩册时才跑）──
    print("\n── ③ 完成统计解析 ──")
    c3 = Course(name="ELA", url="", class_id="11511734")
    sc._apply_gradebook(BeautifulSoup(PAGE_WITH_GRADEBOOK, "html.parser"), c3)
    chk((c3.submitted_count, c3.late_count, c3.pending_count) == (16, 0, 0),
        "16 Submitted / 0 Late / 0 Pending",
        (c3.submitted_count, c3.late_count, c3.pending_count))

    # ── ④ 顺序：先 core_tasks，没有再 units ──
    print("\n── ④ 两页都试的顺序 ──")
    src = (ROOT / "app" / "scraper.py").read_text(encoding="utf-8")
    i_core = src.find("core_soup is not None")
    i_units = src.find('course.url + "/units"')
    chk(i_core > 0, "先试 /core_tasks（已抓到的页面直接复用）")
    chk(i_units > 0, "/units 作为兜底")
    chk(0 < i_core < i_units, "顺序正确：core_tasks 在 units 之前",
        (i_core, i_units))

    print()
    print("=" * 76)
    print(f"  {PASS} 通过 / {FAIL} 失败")
    print("=" * 76)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
