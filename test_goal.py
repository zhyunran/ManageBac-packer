"""成绩目标模拟的契约测试（离线）。

锁住一件事：**`graded` 必须真的影响结论**。

背景：`goal.py` 的文件头写着「已评分类别（graded=True 且有 percent）」，
但实现里只判了 `percent is not None`，完全没读 `graded`。
把 graded 取反，结果一模一样 —— 也就是说：

  · 文档承诺的语义没实现；
  · 一旦出现「页面已有百分比、但该类别并未结束」的情形，
    就会把它当成已锁定，可能给出「这门课的成绩已经定了」这种**反了的结论**。

这个测试用「同一份输入、只改 graded」的两组数据对比，确保结论不同。
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

from app import goal  # noqa: E402

PASS = 0
FAIL = 0


def chk(ok: bool, label: str, extra=None) -> None:
    global PASS, FAIL
    print(f"  {'[OK]' if ok else '[X] '} {label}" + ("" if ok else f"   → {extra!r}"))
    if ok:
        PASS += 1
    else:
        FAIL += 1


def main() -> int:
    print("=" * 70)
    print("  成绩目标模拟契约")
    print("=" * 70)

    # ── ① graded=False 的类别不能被当成「已锁定」──
    print("\n── ① graded=False 但有 percent：应算未出分 ──")
    cats_all_ungraded = [
        {"name": "A", "weight": 20, "percent": 90.0, "graded": False},
        {"name": "B", "weight": 40, "percent": 60.0, "graded": False},
        {"name": "C", "weight": 40, "percent": None, "graded": False},
    ]
    r = goal.simulate(cats_all_ungraded, 90)
    chk(r.get("ok"), "能算出结果", r.get("reason"))
    #  全部未出分 → 没有已锁定部分，held 必须是 0
    chk(r.get("held") == 0.0, "held = 0（没有已锁定分数）", r.get("held"))
    chk(r.get("adjustable") == 100.0, "adjustable = 100（全部可调整）", r.get("adjustable"))
    chk(r.get("status") != "fixed",
        "结论不是「成绩已经定了」（graded=False 就该还能改）", r.get("status"))
    chk(r.get("status") == "possible",
        "结论 = possible（需要 90 分）", r.get("status"))

    # ── ② 同一份数据，只把 graded 翻成 True：结论必须变 ──
    print("\n── ② 同一输入、只把 graded 取反 → 结论必须不同 ──")
    cats_graded = [
        {"name": "A", "weight": 20, "percent": 90.0, "graded": True},
        {"name": "B", "weight": 40, "percent": 60.0, "graded": True},
        {"name": "C", "weight": 40, "percent": None, "graded": False},
    ]
    r2 = goal.simulate(cats_graded, 90)
    chk(r2.get("held") == 42.0, "held = 42（20×90% + 40×60% = 42）", r2.get("held"))
    chk(r.get("held") != r2.get("held"),
        "graded 影响 held（修复前两者相同，就是本测试要抓的 bug）",
        (r.get("held"), r2.get("held")))

    # ── ③ 兼容：不传 graded 时退回老行为（有 percent 即已出分）──
    print("\n── ③ 兼容老调用方：缺 graded 时按 percent 推断 ──")
    legacy = [
        {"name": "A", "weight": 50, "percent": 80.0},
        {"name": "B", "weight": 50, "percent": None},
    ]
    r3 = goal.simulate(legacy, 85)
    chk(r3.get("held") == 40.0, "held = 40（50×80%）", r3.get("held"))
    chk(r3.get("adjustable") == 50.0, "adjustable = 50", r3.get("adjustable"))

    # ── ④ 全部已出分且没达标 → fixed ──
    print("\n── ④ 全部已出分、没达标 → fixed ──")
    r4 = goal.simulate([
        {"name": "A", "weight": 50, "percent": 70.0, "graded": True},
        {"name": "B", "weight": 50, "percent": 70.0, "graded": True},
    ], 90)
    chk(r4.get("status") == "fixed", "status = fixed", r4.get("status"))
    chk(r4.get("adjustable") == 0.0, "没有可调整的部分", r4.get("adjustable"))

    # ── ⑤ 已达标 → met（注意：要区分「全已出分的 met」与「还有可调整部分但已够」）
    print("\n── ⑤ 已出分部分就够 → met ──")
    #  已出分 60 权重 × 90% = 54，目标 50 → 已够；另有 40 权重未出分
    r5 = goal.simulate([
        {"name": "A", "weight": 60, "percent": 90.0, "graded": True},
        {"name": "B", "weight": 40, "percent": None, "graded": False},
    ], 50)
    chk(r5.get("status") == "met", "status = met", r5.get("status"))
    chk(r5.get("held") == 54.0, "held = 54（60×90%）", r5.get("held"))
    chk(r5.get("adjustable") == 40.0,
        "adjustable = 40（还有可调整部分，与「全已出分」是两种 met）",
        r5.get("adjustable"))

    # ── ⑥ 达不到 → unreachable ──
    print("\n── ⑥ 剩余全满分也不够 → unreachable ──")
    r6 = goal.simulate([
        {"name": "A", "weight": 90, "percent": 10.0, "graded": True},
        {"name": "B", "weight": 10, "percent": None, "graded": False},
    ], 90)
    chk(r6.get("status") == "unreachable", "status = unreachable", r6.get("status"))

    # ── ⑦ 权重不是 100 时不猜 ──
    print("\n── ⑦ 权重合计不是 100% → 如实说算不了 ──")
    r7 = goal.simulate([
        {"name": "A", "weight": 30, "percent": 80.0, "graded": True},
    ], 90)
    chk(r7.get("ok") is False, "ok = False", r7)
    chk("100" in (r7.get("reason") or ""), "理由里说明权重问题", r7.get("reason"))

    print()
    print("=" * 70)
    print(f"  {PASS} 通过 / {FAIL} 失败")
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
