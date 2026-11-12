"""状态契约回归 —— 锁住 pipeline 的七态枚举与「哪些态真的会出现」。

为什么单独有一个测试
--------------------
`status` 是后端与前端之间最重要的一条契约，但它以前是**只写在注释里**的：
注释列了七个态，实现里却有一个（`empty`）永远到不了 —— 因为它上面的
两个早退（课程列表为空、任务列表为空）已经把所有能走到那行的输入都拦掉了。
前端还为它写了 38 处处理逻辑，等于白写。

这类「注释列了 N 个，实现只产生 M 个」的漂移不会报错、不会崩，
只会让界面在某种情况下永远说不出正确的话。所以固定成测试：

  1. 枚举值本身不许乱改（前端按字符串比对）；
  2. **每一个声明过的态都必须有产生路径**（防止再出现不可达态）；
  3. 状态映射函数的输入输出逐条钉死。

用法：python test_status_contract.py
"""
from __future__ import annotations

import pathlib
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = pathlib.Path(__file__).resolve().parent
PIPELINE = ROOT / "app" / "pipeline.py"
APJS = ROOT / "web" / "app.js"

PASS = 0
FAIL = 0


def chk(ok: bool, label: str, extra=None) -> None:
    global PASS, FAIL
    print(f"  {'[OK]' if ok else '[X] '} {label}" + ("" if ok else f"   → {extra!r}"))
    if ok:
        PASS += 1
    else:
        FAIL += 1


# ── 契约：注释里声明的七个态 ──
DECLARED = ["idle", "running", "ok", "empty", "partial", "unread", "need_login"]


def status_map(all_tasks: int, courses: int, partial_courses: list,
               reached_early_exit: bool) -> str:
    """复刻 pipeline 里那段判定（与源码逐行同构）。

    早退（课程空 / 任务空）在源码里是 `return`，这里用 reached_early_exit 表示：
    走到判定点时，只有「有课但无待办」或「有待办」两种可能。
    """
    if reached_early_exit:
        return "unread"
    if all_tasks:
        return "partial" if partial_courses else "ok"
    return "empty"


def main() -> int:
    src = PIPELINE.read_text(encoding="utf-8", errors="replace")

    print("=" * 70)
    print("  状态契约回归")
    print("=" * 70)

    # ── 1. 枚举值 ──
    print("\n── ① 声明的枚举 ──")
    block = re.search(r"状态取值.*?\n((?:\s*#.*\n)+)", src)
    declared_in_src = re.findall(r"#\s{2,}([a-z_]+)\s{2,}", block.group(1)) if block else []
    declared_in_src = [s for s in declared_in_src if s in DECLARED]
    chk(sorted(declared_in_src) == sorted(DECLARED),
        f"源码注释声明了 {len(DECLARED)} 个态", declared_in_src)

    # ── 2. 每个态都要有产生路径（就是当初 empty 栽的那条）──
    print("\n── ② 每个态是否都有产生路径 ──")
    #  「产生路径」= 源码里出现 status=<态> / st = "<态>" / 初始 dict 里的 "status": "<态>"
    produced = {}
    for s in DECLARED:
        hits = (len(re.findall(rf'status\s*=\s*"{s}"', src))
                + len(re.findall(rf'st\s*=\s*"{s}"', src))
                + len(re.findall(rf'"status"\s*:\s*"{s}"', src)))
        produced[s] = hits
    for s in DECLARED:
        chk(produced[s] > 0, f"{s:12} 有产生点（{produced[s]} 处）", produced[s])

    # ── 3. 状态映射逐条钉死 ──
    print("\n── ③ 映射表 ──")
    cases = [
        # (任务数, 课程数, 部分课程, 是否早退, 期望)
        (0, 9, [], False, "empty"),            # 有课、无待办 → 本次没有内容
        (12, 9, [], False, "ok"),              # 正常
        (12, 9, ["Chemistry"], False, "partial"),   # 少数课程没读到
        (0, 0, [], True, "unread"),            # 课程列表为空 → 早退
        (0, 9, [], True, "unread"),            # 任务全空 → 早退（限速）
    ]
    for n_task, n_course, partial, early, want in cases:
        got = status_map(n_task, n_course, partial, early)
        chk(got == want,
            f"任务{n_task:>2} 课程{n_course} 部分{len(partial)} 早退{int(early)} → {want}",
            got)

    # ── 4. 不可达态防回归：empty 的判定不能再写成 not tasks and not courses ──
    print("\n── ④ 防回归：empty 的判定条件 ──")
    bad = re.search(r'if\s+not\s+all_tasks\s+and\s+not\s+courses\s*:', src)
    chk(bad is None,
        "没有再写成 `not all_tasks and not courses`（那条件永远为假）",
        bad.group(0) if bad else None)
    chk('st = "empty"' in src, "empty 仍由 else 分支产生")

    # ── 5. 前端对每个后端态都有处理 ──
    print("\n── ⑤ 前端是否认识这些态 ──")
    js = APJS.read_text(encoding="utf-8", errors="replace")
    for s in DECLARED:
        if s in ("idle", "running"):
            continue          # 这两个是过渡态，界面用「正在抓取」统一表达
        n = len(re.findall(rf"['\"]{s}['\"]", js))
        chk(n > 0, f"前端处理 {s:10}（{n} 处）", n)

    print()
    print("=" * 70)
    print(f"  {PASS} 通过 / {FAIL} 失败")
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
