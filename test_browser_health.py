"""浏览器依赖体检的回归测试（离线，用临时目录，不碰真实环境）。

钉住三件事
----------
1. `find_edge()` 能找到 Edge，且候选路径**覆盖**非标准安装位置
   （以前只有三行硬编码，落空就静默退回系统浏览器）。
2. `find_webview2()` 独立检测 —— Edge 与 WebView2 是两个组件。
3. `browser_health()` 在「缺 Edge」时**如实报告**，而不是报 ok。
   这是本次要修的核心：缺 Edge 时跳转、Teams、EC、附件下载一起失灵，
   以前界面上没有任何提示。
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from app import edge  # noqa: E402

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
    print("=" * 74)
    print("  浏览器依赖体检")
    print("=" * 74)

    # ── ① 能定位到一个 Chromium 浏览器（不再限定 Edge）──
    print("\n── ① 浏览器定位（任意 Chromium）──")
    exe = edge.find_edge()
    chk(exe is not None, "找到可用的 Chromium 浏览器", exe)
    if exe:
        chk(exe.is_file(), f"路径确实是个文件：{exe}")

    # ── ② 选型必须**不写死**：能枚举多款，也能按名字切换 ──
    print("\n── ② 选型覆盖面（普适性）──")
    from app import browser as br
    inst = br.installed()
    chk(len(inst) >= 1, f"至少枚举到 1 款浏览器（{len(inst)} 款）",
        [n for _k, n, _p in inst])
    chk(all(p.is_file() for _k, _n, p in inst), "枚举出的路径都真实存在")
    for key, name, path in inst:
        chk(br.find(force=key) == path, f"可强制指定 {name}")
    chk(isinstance(br.default_browser_key(), str), "默认浏览器探测不抛异常")

    # ── ③ WebView2 单独检测 ──
    print("\n── ③ find_webview2（与 Edge 分开）──")
    wv2 = edge.find_webview2()
    print(f"      WebView2 = {wv2}")
    chk(hasattr(edge, "find_webview2"), "存在独立的 WebView2 检测函数")
    if wv2:
        chk(wv2.is_file(), "WebView2 路径确实是个文件")

    # ── ④ browser_health 实时体检 ──
    print("\n── ④ browser_health（真实环境）──")
    h = edge.browser_health()
    for k in ("ok", "edge", "webview2", "profile_ready", "missing", "message"):
        chk(k in h, f"返回字段包含 {k}", list(h.keys()))
    chk(isinstance(h.get("missing"), list), "missing 是列表", h.get("missing"))
    chk(h.get("ok") == (not h.get("missing")), "ok 与 missing 一致（ok 就是「什么都不缺」）", h)
    #  ★ 独立配置目录在首次启动前不存在，**不算**不健康 ——
    #    否则每个新用户开机都会看到一条假告警（实测踩过）。
    chk("profile" not in (h.get("missing") or []),
        "独立配置目录缺失不算不健康（首次运行前本来就没有）", h.get("missing"))
    if h.get("edge") and h.get("webview2"):
        chk(h.get("ok") is True, "Edge 与 WebView2 都在 → ok=True", h)
    if h.get("edge"):
        chk(bool(h.get("edge_path")), "给出了 Edge 路径")
    chk("profile_ready" in h, "profile_ready 仍作为诊断字段返回")
    print(f"      缺什么    = {h.get('missing')}")
    print(f"      提示语    = {h.get('message')}")

    # ── ⑤ 关键：缺 Edge 时必须如实报告（用假探针模拟）──
    print("\n── ⑤ 缺 Edge 时要如实报告，不能报 ok ──")
    real_find = edge.find_edge
    real_wv2 = edge.find_webview2
    try:
        edge.find_edge = lambda: None                      # 假装一个浏览器都没装
        edge.find_webview2 = lambda: pathlib.Path("x")     # 假装 WebView2 在
        h2 = edge.browser_health()
        chk(h2.get("ok") is False, "缺浏览器 → ok=False（不再静默当正常）", h2)
        chk("browser" in (h2.get("missing") or []),
            "missing 里点名 browser（不再只认 edge）", h2.get("missing"))
        msg = h2.get("message") or ""
        chk("Teams" in msg and "附件" in msg,
            "提示语说清了影响面（不只是「跳转」不可用）", msg)
        print(f"      提示语 = {msg}")
    finally:
        edge.find_edge = real_find
        edge.find_webview2 = real_wv2

    # ── ⑥ 缺 WebView2 是另一条独立提示 ──
    print("\n── ⑥ 只有 WebView2 缺失时，提示语不同 ──")
    try:
        edge.find_edge = lambda: pathlib.Path("fake_msedge.exe")
        edge.find_webview2 = lambda: None
        h3 = edge.browser_health()
        chk("webview2" in (h3.get("missing") or []), "missing 里点名 webview2", h3.get("missing"))
        chk("WebView2" in (h3.get("message") or ""), "提示语提到 WebView2", h3.get("message"))
        chk(h3.get("ok") is False, "缺 WebView2 也不是 ok")
    finally:
        edge.find_edge = real_find
        edge.find_webview2 = real_wv2

    print()
    print("=" * 74)
    print(f"  {PASS} 通过 / {FAIL} 失败")
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
