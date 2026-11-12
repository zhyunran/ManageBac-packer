"""提交状态解析的回归测试（离线，用探测到的真实 HTML 结构）。

背景（2026-10-08 实测）
----------------------
`parse_status` 原本写成一个「互斥分类器」，三个判断依次 return：

    if 含 late 词   → return "迟交", False, ...     ← 先命中
    if 含 pending   → return "待提交", False, ...
    if 含 submitted → return "已提交", True, ...

而 ManageBac 上「迟交但交了」的徽章长这样（真实探测结果）：

    <span class="badge color-box-orange" data-bs-title="1 hour late">
      <span class="badge-label">Submitted</span></span>

拼出来的 blob 是 "submitted | 1 hour late"，第一个判断就命中 late 并
直接 return，`submitted` 被硬写成 False。于是：

  · 界面上那门**已经交了**的作业顶着「提交：未提交」的标签；
  · `completed` 依赖 submitted/graded，于是它看起来像没交。
  用户的原话就是「我明明提交了，工具里不显示」。

「交没交」与「迟没迟」是两件独立的事。这个测试把它们钉死。
"""
from __future__ import annotations

import pathlib
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from bs4 import BeautifulSoup  # noqa: E402

from app.scraper import parse_status  # noqa: E402

PASS = 0
FAIL = 0


def chk(ok: bool, label: str, extra=None) -> None:
    global PASS, FAIL
    print(f"  {'[OK]' if ok else '[X] '} {label}" + ("" if ok else f"   → {extra!r}"))
    if ok:
        PASS += 1
    else:
        FAIL += 1


def card(inner: str):
    """按真实结构包一张卡片：提交徽章在 .labels-set，成绩在 .assessment-cell
    （实测：`<div class="assessment task-score ... assessment-cell">`）。"""
    return BeautifulSoup(f'<div class="card">{inner}</div>',
                         "html.parser").select_one(".card")


def badge(label: str, title: str = "") -> str:
    t = f' data-bs-title="{title}"' if title else ""
    return (f'<div class="labels-set"><span class="badge"{t}>'
            f'<span class="badge-label">{label}</span></span></div>')


def assessment(inner: str) -> str:
    return f'<div class="assessment task-score assessment-cell">{inner}</div>'


# ── 真实结构（来自 CDP 探测，见 _probe_out/报告.txt）──
LATE_SUBMITTED = badge("Submitted", "1 hour late") + \
    assessment('<div class="cell icon not-assessed">Not Assessed Yet</div>')
ON_TIME_SUBMITTED = badge("Submitted", "3 days early")
PENDING = badge("Pending", "Waiting") + \
    assessment('<div class="cell not-submitted">Not Submitted</div>')
NOT_SUBMITTED = assessment('<div class="cell not-submitted">Not Submitted</div>')
EMPTY = '<div class="labels"></div>'


def main() -> int:
    print("=" * 74)
    print("  提交状态解析契约")
    print("=" * 74)

    # ── ① 本次修复的核心：迟交 ≠ 没交 ──
    print("\n── ① 迟交但已提交（本次修复的核心）──")
    r = parse_status(card(LATE_SUBMITTED))
    chk(r[1] is True, "submitted=True（交了的作业不能报成没交）", r)
    chk(r[3] is True, "late=True", r)
    chk(r[0] == "迟交", "状态显示「迟交」（信息量比「已提交」更大）", r[0])
    chk(r[2] is False, "pending=False（已经交了，不是待提交）", r)

    #  ── ② 按时提交 ──
    print("\n── ② 按时已提交 ──")
    r = parse_status(card(ON_TIME_SUBMITTED))
    chk(r[1] is True and r[3] is False, "submitted=True, late=False", r)
    chk(r[0] == "已提交", "状态 = 已提交", r[0])

    # ── ③ 明确未提交 ──
    print("\n── ③ Pending + Not Submitted ──")
    r = parse_status(card(PENDING))
    chk(r[1] is False, "submitted=False", r)
    chk(r[2] is True, "pending=True", r)
    r2 = parse_status(card(NOT_SUBMITTED))
    chk(r2[1] is False and r2[2] is True, "只有 Not Submitted 单元格也算待提交", r2)

    # ── ④ 没找到标记时不下结论（不要把「没认出来」说成「你没交」）──
    print("\n── ④ 页面上没有任何标记 ──")
    r = parse_status(card(EMPTY))
    chk(r[0] == "", "status 留空（不编造）", r)
    chk(r[1] is False and r[2] is False, "两个标记都不置位", r)

    # ── ⑤ 中文 ──
    print("\n── ⑤ 中文「已提交」──")
    r = parse_status(card(badge("已提交")))
    chk(r[1] is True and r[0] == "已提交", "中文也认", r)

    # ── ⑥ 「not submitted」里含 submitted 子串，不能误判成交了 ──
    print("\n── ⑥ 子串陷阱：Not Submitted 含 'submitted' ──")
    r = parse_status(card(NOT_SUBMITTED))
    chk(r[1] is False, "Not Submitted 不能被当成 Submitted", r)

    print()
    print("=" * 74)
    print(f"  {PASS} 通过 / {FAIL} 失败")
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
