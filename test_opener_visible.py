"""「打开原网页」的可见性回归测试。

用户反馈
--------
「你自己试试打开原网页？没反应，还显示打开了。」

根因
----
抓取用的 Edge 是 ``--headless`` 起的 —— 它**没有窗口**。
点跳转时通过 CDP 往这个实例里新开标签：调用确实成功，接口如实返回
「已打开」，但标签开在一个看不见的浏览器里。界面于是出现
「提示打开了、屏幕毫无变化」。

修法
----
① 先用 CDP 的 ``/json/version`` 判断在跑的实例是不是无头
   （无头实例的 User-Agent 里带 ``Headless``）
② 是 → 收掉它，另起一个**可见窗口**
   （同一个 user-data-dir 有单实例锁，不能并存）
③ 不是 → 新开标签，并把窗口叫到前面（最小化的先还原）

本测试盯住 ① 的判定与 ③ 的分支，不启真浏览器。
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


HEADLESS_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) HeadlessChrome/156.0.0.0 Safari/537.36 "
               "Edg/156.0.0.0")
NORMAL_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36 "
             "Edg/156.0.0.0")


def main() -> int:
    print("=" * 76)
    print("  打开原网页：无头实例判定与分支")
    print("=" * 76)

    from app import opener

    real = opener._cdp_json

    def fake_json(payload):
        def _f(port, path, method="GET", timeout=6.0):
            return payload
        return _f

    try:
        # ── ① 无头判定 ──
        print("\n── ① 是不是无头实例 ──")
        opener._cdp_json = fake_json({"User-Agent": HEADLESS_UA,
                                      "Browser": "Edg/156.0.4314.50"})
        chk(opener._instance_headless(1234) is True,
            "User-Agent 带 Headless → 判为无头")

        opener._cdp_json = fake_json({"User-Agent": NORMAL_UA,
                                      "Browser": "Edg/156.0.4314.50"})
        chk(opener._instance_headless(1234) is False,
            "普通 User-Agent → 判为有窗口")

        opener._cdp_json = fake_json({"Browser": "HeadlessEdg/156.0"})
        chk(opener._instance_headless(1234) is True,
            "Browser 字段带 Headless 也算")

        opener._cdp_json = fake_json(None)
        chk(opener._instance_headless(1234) is None,
            "CDP 查不到 → 返回 None（不武断）")

        opener._cdp_json = fake_json({})
        chk(opener._instance_headless(1234) is None,
            "没有 User-Agent → None")

        # ── ② 无头时不得往里开标签 ──
        print("\n── ② 无头实例：不许往里开标签，必须另起可见窗口 ──")
        calls = {"new_tab": 0, "closed": 0, "launched": 0, "focus": 0}

        class FakeEdge:
            @staticmethod
            def find_edge():
                return r"C:\Edge\msedge.exe"

            @staticmethod
            def read_devtools_port(prof):
                return 57422

            @staticmethod
            def is_edge_running(prof):
                return True

            @staticmethod
            def launch_edge(prof, headless=False, url=None, debug_port=True):
                calls["launched"] += 1
                calls["headless_arg"] = headless

                class P:
                    pid = 4242
                return P()

        real_edge = opener.edge
        opener.edge = FakeEdge

        def fake_new_tab(port, url):
            calls["new_tab"] += 1
            return "target-1"

        real_new = opener._cdp_new_tab
        real_focus = opener._focus_browser_window
        real_activate = opener._cdp_activate
        opener._cdp_new_tab = fake_new_tab
        opener._cdp_activate = lambda port, tid: True

        def fake_focus(prof, pid_hint=0, tries=10):
            calls["focus"] += 1
            return True

        opener._focus_browser_window = fake_focus

        #  装一个假的 cdp 模块，记录有没有去关无头实例
        import types
        fake_cdp = types.ModuleType("app.cdp")
        fake_cdp.close_profile_browser = lambda prof, timeout=12: (
            calls.__setitem__("closed", calls["closed"] + 1) or 1)
        fake_cdp.list_profile_pids = lambda prof: []
        sys.modules["app.cdp"] = fake_cdp
        real_cdp = getattr(opener, "cdp", None)

        opener._cdp_json = fake_json({"User-Agent": HEADLESS_UA})
        r = opener.open_in_own_profile("https://x.test/a", reuse=True)
        chk(r.get("ok") is True, "整体返回成功", r)
        chk(r.get("how") == "launched",
            f"走的是「新起可见窗口」这条路（how={r.get('how')}）", r.get("how"))
        chk(calls["new_tab"] == 0,
            f"没有往无头实例里开标签（new_tab={calls['new_tab']}）",
            calls["new_tab"])
        chk(calls["closed"] >= 1, f"把无头实例收掉了（{calls['closed']} 次）",
            calls["closed"])
        chk(calls["launched"] == 1, f"新起了 {calls['launched']} 个进程",
            calls["launched"])
        chk(calls.get("headless_arg") is False,
            "新起的实例**不是**无头（headless=False）",
            calls.get("headless_arg"))
        chk(calls["focus"] >= 1, "并尝试把窗口叫到前面", calls["focus"])

        # ── ③ 有窗口时复用 + 前置 ──
        print("\n── ③ 有窗口的实例：复用标签并前置 ──")
        calls.update({"new_tab": 0, "closed": 0, "launched": 0, "focus": 0})
        opener._cdp_json = fake_json({"User-Agent": NORMAL_UA})
        r2 = opener.open_in_own_profile("https://x.test/b", reuse=True)
        chk(r2.get("how") == "reused",
            f"走复用这条路（how={r2.get('how')}）", r2.get("how"))
        chk(calls["new_tab"] == 1, "新开了一个标签", calls["new_tab"])
        chk(calls["closed"] == 0, "没有关掉可见实例", calls["closed"])
        chk(calls["focus"] >= 1, "把窗口叫到前面了", calls["focus"])
        chk(r2.get("focused") is True, "结果里带上 focused=True", r2.get("focused"))

        opener._focus_browser_window = real_focus
        opener._cdp_new_tab = real_new
        opener._cdp_activate = real_activate
        opener.edge = real_edge
        if real_cdp is None:
            sys.modules.pop("app.cdp", None)

    finally:
        opener._cdp_json = real

    print()
    print("=" * 76)
    print(f"  {PASS} 通过 / {FAIL} 失败")
    print("=" * 76)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
