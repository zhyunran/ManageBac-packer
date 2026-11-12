"""晚报翻期的回归测试。

用户反馈
--------
「晚报点击往次后无法跳转到其他时间/当前晚报。原因：无按钮。」

根因
----
`digest_by_date` 原来只返回那一期本身，**不带 history**。
而界面上的「往期晚报」按钮行（以及现在的期数导航）依赖 history 才渲染 ——
点进往期之后那部分直接消失，面板就卡在某一期上。

本测试固定三件事：
  ① 按日期取一期时，一定带上完整 history 与 latest_date / is_latest
  ② 最新一期与往期取到的 history 是**同一份**（否则切一次导航就断）
  ③ 历史按日期升序，且能找到正确的「上一期 / 下一期」
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


ISSUES = [
    {"date": "2026-10-28", "weekday": "周三", "counts": {"new_tasks": 2},
     "generated": "21:00", "counts_extra": 1},
    {"date": "2026-10-29", "weekday": "周四", "counts": {"new_grades": 1},
     "generated": "21:00"},
    {"date": "2026-10-30", "weekday": "周五", "counts": {"new_tasks": 3},
     "generated": "21:00"},
]


def main() -> int:
    print("=" * 76)
    print("  晚报翻期：history 必须始终在场")
    print("=" * 76)

    import widget
    from app import digest, pipeline

    real_list_all = digest.list_all
    real_latest = digest.latest
    real_ensure = digest.ensure_today
    real_snapshot = pipeline.snapshot

    #  后端返回倒序（真实实现就是新的在前），代码里必须自己排好
    digest.list_all = lambda n=60: [dict(x) for x in reversed(ISSUES)]
    digest.latest = lambda: dict(ISSUES[-1])
    digest.ensure_today = lambda data: None
    pipeline.snapshot = lambda: {"tasks": [], "courses": []}

    api = widget.Api()
    try:
        # ── ① 按日期取往期：必须带 history ──
        print("\n── ① 取往期：history 与 latest_date 都在 ──")
        r = api.digest_by_date("2026-10-29")
        chk(r.get("ok") is True, "返回 ok", r.get("ok"))
        chk("history" in r, "带 history 字段", list(r.keys())[:8])
        chk(len(r.get("history") or []) == 3,
            f"history 有 3 期（实际 {len(r.get('history') or [])}）",
            [x.get("date") for x in (r.get("history") or [])])
        chk(r.get("latest_date") == "2026-10-30",
            f"latest_date={r.get('latest_date')!r}", r.get("latest_date"))
        chk(r.get("is_latest") is False, "往期 is_latest=False", r.get("is_latest"))

        # ── ② 取最新一期：同样带 history ──
        print("\n── ② 取最新：字段一致 ──")
        r2 = api.digest_latest()
        chk(r2.get("ok") is True, "返回 ok", r2.get("ok"))
        chk(len(r2.get("history") or []) == 3, "history 也是 3 期",
            len(r2.get("history") or []))
        chk(r2.get("latest_date") == "2026-10-30", "latest_date 正确",
            r2.get("latest_date"))
        chk(r2.get("is_latest") is True, "最新 is_latest=True", r2.get("is_latest"))

        # ── ③ 两份 history 必须一致 ──
        print("\n── ③ 最新与往期拿到的 history 是同一份 ──")
        a = [x["date"] for x in (r.get("history") or [])]
        b = [x["date"] for x in (r2.get("history") or [])]
        chk(a == b, "两份列表完全相同（切期不会丢导航）", (a, b))
        chk(a == sorted(a), f"按日期升序：{a}", a)
        chk(a == ["2026-10-28", "2026-10-29", "2026-10-30"],
            "顺序从旧到新", a)

        # ── ④ 上一期 / 下一期 的推算 ──
        print("\n── ④ 上一期 / 下一期 可推算 ──")
        hist = a
        i = hist.index("2026-10-29")
        prev = hist[i - 1] if i > 0 else None
        nxt = hist[i + 1] if i < len(hist) - 1 else None
        chk(prev == "2026-10-28", f"29 号的上一期是 {prev}", prev)
        chk(nxt == "2026-10-30", f"29 号的下一期是 {nxt}", nxt)
        chk(hist.index("2026-10-28") == 0, "最早一期没有上一期")
        chk(hist.index("2026-10-30") == len(hist) - 1, "最新一期没有下一期")

        # ── ⑤ 找不到的日期要如实报错 ──
        print("\n── ⑤ 不存在的日期 ──")
        r5 = api.digest_by_date("2020-01-01")
        chk(r5.get("ok") is False, "返回 ok=False", r5.get("ok"))
        chk(bool(r5.get("error")), f"带上原因：{r5.get('error')}",
            r5.get("error"))

        # ── ⑥ history 为空时不炸 ──
        print("\n── ⑥ 一期都没有时不炸 ──")
        digest.list_all = lambda n=60: []
        digest.latest = lambda: None
        r6 = api.digest_by_date("2026-10-30")
        chk(r6.get("ok") is False, "取不到就返回失败", r6.get("ok"))
        chk(isinstance(widget._digest_history(digest), list),
            "_digest_history 返回列表而不是异常")
        chk(widget._digest_history(digest) == [], "空列表")

    finally:
        digest.list_all = real_list_all
        digest.latest = real_latest
        digest.ensure_today = real_ensure
        pipeline.snapshot = real_snapshot

    print()
    print("=" * 76)
    print(f"  {PASS} 通过 / {FAIL} 失败")
    print("=" * 76)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
