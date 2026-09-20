"""连接 / 登录 / 抓取 —— 一体化命令行工具。

    uv run connect.py            # 检查登录状态；必要时打开窗口登录
    uv run connect.py --force    # 强制重新登录
    uv run connect.py --fetch    # 登录后立刻抓取一次数据
    uv run connect.py --probe    # 抓取并打印详细结果

【工作原理】
    程序启动一个真实 Edge 窗口，你在里面手动登录。
    启动参数已包含 `--disable-blink-features=AutomationControlled`，
    因此即使开着调试端口，navigator.webdriver 仍为 false —— 不会被风控识别。

    登录态由 Edge 自己保存在 data/browser-profile，之后程序可直接复用。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import cdp, config  # noqa: E402

HOME = f"{config.BASE_URL}/student/home"


def is_logged_in() -> bool:
    """只读探测登录状态。"""
    if not cdp.is_running(config.BROWSER_PROFILE_DIR):
        return False
    try:
        s = cdp.Session(headless=True)
        try:
            s.open()
            page = s.page()
            page.goto(HOME)
            page.wait_for_timeout(1500)
            return cdp.looks_logged_in(page)
        finally:
            s.close()
    except Exception:
        return False


def open_login_window() -> None:
    """打开真实 Edge 窗口供登录。"""
    if cdp.is_running(config.BROWSER_PROFILE_DIR):
        print("  [i] 窗口已在运行，请直接在其中登录。")
        return
    print("  正在打开 Edge 窗口……")
    cdp.launch(config.BROWSER_PROFILE_DIR, headless=False, url=HOME)


def wait_login(timeout: float = 600.0) -> bool:
    last = -20
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(3)
        elapsed = int(time.time() - t0)
        if elapsed - last >= 20:
            last = elapsed
            print(f"     … 等待登录中（已 {elapsed} 秒）")
        if is_logged_in():
            return True
    return False


def run_fetch(probe: bool) -> int:
    """抓取数据并打印结果。"""
    from app import pipeline, store

    print()
    print("  ── 开始抓取 ──")

    done = {"ok": False}

    orig = pipeline._set

    def spy(**kw):
        orig(**kw)
        if "message" in kw and kw["message"]:
            print(f"     {kw['message']}")
        if kw.get("status") in ("ok", "error", "need_login"):
            done["ok"] = kw["status"] == "ok"

    pipeline._set = spy  # type: ignore
    try:
        pipeline.refresh_sync()
    finally:
        pipeline._set = orig  # type: ignore

    snap = pipeline.snapshot()
    print()
    print("=" * 74)
    if snap["status"] != "ok":
        print(f"  [X] 抓取未成功：{snap['message']}")
        return 1

    print(f"  [OK] 更新于 {snap['updated']}")
    print(f"       任务 {len(snap['tasks'])} 个 / 课程 {len(snap['courses'])} 门")
    print()

    if probe:
        tasks = [t.to_dict() if hasattr(t, "to_dict") else t for t in snap["tasks"]]
        courses = [c.to_dict() if hasattr(c, "to_dict") else c for c in snap["courses"]]

        print("-" * 74)
        print("  任务明细")
        print("-" * 74)
        for t in sorted(tasks, key=lambda x: x["days_left"]
                        if x["days_left"] is not None else 999):
            dl = t["days_left"]
            flag = "!!" if (dl is not None and dl < 0) else "  "
            print(f"  {flag} {t['title'][:36]:<36} {t['course'][:16]:<16} "
                  f"截止 {t['due_date'] or '—':<11} {t['score'] or ''}")
        print()
        print("-" * 74)
        print("  课程成绩")
        print("-" * 74)
        for c in courses:
            lv = c["overall_level"] or "—"
            pc = c["overall_percent"]
            print(f"  {c['name'][:40]:<40} {lv:<3} "
                  f"{f'{pc:.2f}%' if pc is not None else '—':<9} "
                  f"(任务 {len(c['tasks'])}, 已交 {c['submitted_count']}, "
                  f"迟交 {c['late_count']}, 待交 {c['pending_count']})")
            for cat in c["categories"]:
                if cat["name"].lower() == "overall":
                    continue
                w = f"{cat['weight']:g}%" if cat["weight"] is not None else "—"
                v = (f"{cat['level']} {cat['percent']:.2f}%"
                     if cat["graded"] else "未出分")
                print(f"      · {cat['name'][:18]:<18} 权重 {w:<6} {v}")
        print()
        s = snap["summary"]
        if s.get("mean_percent") is not None:
            print(f"  平均分（各科总评平均）：{s['mean_percent']}%")

    print()
    print("=" * 74)
    print("  下一步：uv run widget.py    # 启动桌面小组件")
    print("=" * 74)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="强制重新登录")
    ap.add_argument("--fetch", action="store_true", help="登录后立刻抓取")
    ap.add_argument("--probe", action="store_true", help="抓取并打印明细")
    ap.add_argument("--timeout", type=int, default=600)
    args = ap.parse_args()

    config.ensure_dirs()

    print("=" * 74)
    print("  ManageBac 连接工具")
    print("=" * 74)

    exe = cdp.find_edge()
    if exe is None:
        print("  [X] 未找到 msedge.exe")
        return 1
    print(f"  Edge     : {exe}")
    print(f"  数据目录 : {config.BROWSER_PROFILE_DIR}")
    print()

    # ---- 检查登录 ----
    if not args.force:
        print("  正在检查登录状态……")
        if is_logged_in():
            print("  [OK] 已登录，可直接使用。")
            if args.fetch or args.probe:
                return run_fetch(args.probe)
            print()
            print("  可选：")
            print("     uv run connect.py --fetch   # 抓取数据")
            print("     uv run widget.py            # 启动小组件")
            print("     uv run connect.py --force   # 换账号重新登录")
            return 0
        print("  [i] 尚未登录（或登录已过期）。")

    # ---- 登录流程 ----
    print()
    print("  ┌──────────────────────────────────────────────────────────┐")
    print("  │  请在弹出的 Edge 窗口里，正常输入账号密码登录。           │")
    print("  │  这个窗口与你平时用的 Edge 一致，不会被风控识别。         │")
    print("  │  登录成功后无需关闭窗口，程序会自动检测到。               │")
    print("  └──────────────────────────────────────────────────────────┘")
    print()

    open_login_window()
    if not wait_login(args.timeout):
        print()
        print("  [X] 未检测到登录（超时）。")
        print("      若窗口里提示密码错误，请确认账号本身可正常登录。")
        return 1

    print("  [OK] 登录成功！")
    if args.fetch or args.probe:
        return run_fetch(args.probe)

    print()
    print("  下一步：")
    print("     uv run connect.py --fetch   # 抓取数据")
    print("     uv run widget.py            # 启动桌面小组件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
