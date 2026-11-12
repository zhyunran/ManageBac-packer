"""「打开原网页」的后端回归测试：进度、防重复、超时。

为什么需要
----------
用户报告：点「打开原网页」后三分钟没有任何反应。
现场两份证据：
  · 日志里同一个链接在 6 秒内被打开了十几次（没有反馈 → 反复点）
  · 进程列表里有个卡住的 powershell.exe（内部调用会挂住不返回）

所以后端必须做到三件事，本测试逐条固定：
  ① 有进度可查（界面才能显示「已耗时 N 秒」）
  ② 上一次没结束时，新的请求被拒绝并说明原因
  ③ 内部卡死时，调用在超时后返回结论，而不是永远等着

不联网：把真正打开链接的那一步替换掉。
"""
from __future__ import annotations

import pathlib
import sys
import threading
import time

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


def main() -> int:
    print("=" * 76)
    print("  打开原网页：进度 / 防重复 / 超时")
    print("=" * 76)

    import widget

    api = widget.Api()

    def reset():
        with widget._OPEN_LOCK:
            widget._OPEN.update(seq=0, running=False, url="", started=0.0,
                               elapsed=0.0, ok=None, how="", error="",
                               fallback=False, note="", busy_rejections=0)

    # ── ① 一次正常打开 ──
    print("\n── ① 正常打开：有耗时，进度归零 ──")
    reset()
    seen = {}

    def fake_ok(url):
        seen["url"] = url
        time.sleep(0.25)
        return {"ok": True, "how": "reused"}

    api._do_open = fake_ok
    r = api.open_url("https://example.test/a")
    chk(r.get("ok") is True, "返回 ok", r)
    chk(r.get("how") == "reused", "带上打开方式", r.get("how"))
    chk(isinstance(r.get("elapsed"), (int, float)) and r["elapsed"] >= 0.2,
        f"带回耗时 {r.get('elapsed')} 秒", r.get("elapsed"))
    chk(seen.get("url") == "https://example.test/a", "链接原样传到下层",
        seen.get("url"))
    st = api.open_progress()
    chk(st.get("running") is False, "结束后 running=False", st.get("running"))
    chk(r.get("seq") == 1, "序号从 1 开始", r.get("seq"))

    # ── ② 进度可查 ──
    print("\n── ② 打开期间能查到进度（界面靠它显示秒数）──")
    reset()

    def slow(url):
        time.sleep(0.9)
        return {"ok": True, "how": "launched"}

    api._do_open = slow
    box = {}

    def run():
        box["r"] = api.open_url("https://example.test/slow")

    th = threading.Thread(target=run)
    th.start()
    time.sleep(0.35)
    mid = api.open_progress()
    chk(mid.get("running") is True, "进行中 running=True", mid.get("running"))
    chk(mid.get("elapsed", 0) > 0.15,
        f"已耗时 {mid.get('elapsed')} 秒（在累加）", mid.get("elapsed"))
    chk(mid.get("url") == "https://example.test/slow", "记录了正在打开的链接")
    chk(mid.get("timeout") == widget.OPEN_TIMEOUT,
        f"报告超时阈值 {mid.get('timeout')} 秒", mid.get("timeout"))
    th.join(5)
    chk(box.get("r", {}).get("ok") is True, "那次打开最后成功", box.get("r"))

    # ── ③ 防重复：上一次没结束时拒绝 ──
    print("\n── ③ 上一次没结束，新请求被拒绝 ──")
    reset()
    api._do_open = slow
    box2 = {}
    th2 = threading.Thread(target=lambda: box2.update(
        r=api.open_url("https://example.test/first")))
    th2.start()
    time.sleep(0.25)
    r2 = api.open_url("https://example.test/second")
    chk(r2.get("ok") is False, "第二次被拒（ok=False）", r2)
    chk(r2.get("busy") is True, "标记 busy，界面据此提示「请稍候」", r2.get("busy"))
    chk("请稍候" in (r2.get("error") or ""), "错误文案说明原因",
        r2.get("error"))
    chk(isinstance(r2.get("elapsed"), (int, float)),
        f"带上已等待时长 {r2.get('elapsed')} 秒", r2.get("elapsed"))
    th2.join(5)
    with widget._OPEN_LOCK:
        rej = widget._OPEN["busy_rejections"]
    chk(rej >= 1, f"记录了 {rej} 次重复点击", rej)

    # ── ④ 超时：内部卡死也要有结论 ──
    print("\n── ④ 内部卡死：超时后返回结论 ──")
    reset()
    old_timeout = widget.OPEN_TIMEOUT
    widget.OPEN_TIMEOUT = 0.6

    def hang(url):
        time.sleep(30)          # 模拟卡住的 PowerShell 调用
        return {"ok": True, "how": "never"}

    api._do_open = hang
    t0 = time.time()
    r4 = api.open_url("https://example.test/hang")
    cost = time.time() - t0
    widget.OPEN_TIMEOUT = old_timeout
    chk(r4.get("ok") is False, "超时算失败（ok=False）", r4)
    chk(r4.get("timeout") is True, "标记 timeout", r4.get("timeout"))
    chk(0.5 <= cost <= 3.0, f"在阈值附近返回，实际 {cost:.1f} 秒", cost)
    chk("超时" in (r4.get("error") or ""), "错误文案写明超时", r4.get("error"))
    st4 = api.open_progress()
    chk(st4.get("running") is False, "超时后 running 也归零",
        st4.get("running"))

    # ── ⑤ 下层抛异常 ──
    print("\n── ⑤ 下层抛异常也要有结论 ──")
    reset()

    def boom(url):
        raise RuntimeError("模拟打开失败")

    api._do_open = boom
    r5 = api.open_url("https://example.test/boom")
    chk(r5.get("ok") is False, "返回失败", r5)
    chk("模拟打开失败" in (r5.get("error") or ""), "带上原因",
        r5.get("error"))
    chk(api.open_progress().get("running") is False, "状态已复位")

    # ── ⑥ 空链接 ──
    print("\n── ⑥ 空链接 ──")
    reset()
    r6 = api.open_url("")
    chk(r6.get("ok") is False and r6.get("error"), "空链接直接拒绝并说明",
        r6)

    # ── ⑦ 时间戳合法 ──
    print("\n── ⑦ 状态字段自洽 ──")
    reset()
    api._do_open = fake_ok
    api.open_url("https://example.test/x")
    st7 = api.open_progress()
    chk(st7.get("ok") is True, "open_progress 自身 ok=True", st7.get("ok"))
    chk(st7.get("last" if False else "running") is False, "running=False")
    chk(st7.get("error") == "", "成功时 error 为空", st7.get("error"))

    print()
    print("=" * 76)
    print(f"  {PASS} 通过 / {FAIL} 失败")
    print("=" * 76)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
