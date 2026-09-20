"""反复启动小组件，确认「未响应」彻底消失。

用法：
    .venv\\Scripts\\python.exe stress_start.py [轮数] [每轮等待秒数]

判定标准（全部满足才算通过）：
    1. 窗口存在，且 Responding = True          ← 核心
    2. 日志出现「界面已就绪（前端首次请求数据）」 ← 界面真的活了
    3. 没有 faulthandler 崩溃痕迹（access violation 等）

背景（2026-09-18 定位）：
    pywebview 注入 JS API 时会遍历 js_api 对象的**所有公开属性**
    （webview/util.py: get_functions），遇到非函数对象就递归进去。
    我们把 `Api.window` 指向了 pywebview 的 Window 对象，于是递归进
    DOM / EventContainer / .NET 窗体，而 Window.width/height 又是
    会 wait(15s) 的属性 getter → 递归爆炸（实测 60 秒）。
    期间 pywebviewready / loaded 事件全被堵住，窗口一直「未响应」。
    解法：改名 `Api._window`（下划线开头会被跳过）→ 0.00 秒。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

ROOT = Path(__file__).resolve().parent
PYW = ROOT / ".venv" / "Scripts" / "pythonw.exe"
LOG = ROOT / "logs" / "widget.log"

# Windows 常量
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


def ps(cmd: str) -> str:
    """跑一段 PowerShell，返回 stdout（UTF-8）。

     必须先把 PowerShell 自己的输出编码设成 UTF-8，
      否则中文标题会被按 GBK 解码 → 乱码。
    """
    wrapped = ("[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
        "$OutputEncoding=[System.Text.Encoding]::UTF8;" + cmd
    )
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", wrapped],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=60,
    )
    return (r.stdout or "").strip()


def kill_all() -> None:
    """只杀 **widget.py** 相关进程（按命令行精确匹配）。

     教训 1：不能按进程名 `python,pythonw` 全杀 —— 会把本脚本自己杀掉，
      脚本静默消失、没有任何输出。
     教训 2：不能只排除 `os.getpid()` —— uv 的 venv 里
      `.venv\\Scripts\\python.exe` 其实是个 **shim**，它会再 spawn 真正的
      解释器。按 pid 排除会漏掉另一个，而杀掉 shim 又会让 PowerShell
      的作业被认为已结束（退出码 -1）。
      所以改成「按命令行里是否含 widget.py」来精确匹配，绝不误杀。
    """
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
         "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
         "Get-CimInstance Win32_Process "
         "-Filter \"Name='python.exe' or Name='pythonw.exe'\" -EA SilentlyContinue "
         "| Where-Object { $_.CommandLine -like '*widget.py*' } "
         "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force -EA SilentlyContinue }"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
    )
    time.sleep(2.0)


def clear_state() -> None:
    for f in (LOG, ROOT / "data" / "scrape.lock"):
        try:
            f.unlink()
        except Exception:
            pass


def read_log() -> str:
    """读 widget.log 全文（读不到就返回空串）。"""
    try:
        return LOG.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def window_state() -> tuple[bool, bool, str]:
    """返回 (找到窗口?, 响应?, 描述)。"""
    out = ps("Get-Process | Where-Object { $_.MainWindowTitle -like '*ManageBac*' "
        "-and $_.ProcessName -notin @('Code','msedge') } | "
        "ForEach-Object { \"$($_.Id)|$($_.Responding)|$($_.MainWindowTitle)\" }"
    )
    if not out:
        return False, False, "(没有窗口)"
    line = out.splitlines()[0].strip()
    parts = line.split("|")
    if len(parts) < 2:
        return False, False, line
    return True, parts[1].strip().lower() == "true", line


def run_one(round_no: int, wait_sec: float) -> bool:
    print(f"\n{'-' * 62}")
    print(f"  第 {round_no} 轮")
    print(f"{'-' * 62}")

    kill_all()
    clear_state()

    t0 = time.time()
    subprocess.Popen([str(PYW), "widget.py"],
        cwd=str(ROOT),
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    print(f"  已启动（{PYW.name}），等 {wait_sec:.0f} 秒 …")

    # 轮询等待：窗口「响应」+ 日志出现「界面已就绪」两个条件都满足才算成功
    #  不能只等「响应」就提前跳出 —— 那时前端可能还没发出第一次 get_data，
    #   日志里还没有「界面已就绪」，会造成假失败。
    found = False
    responding = False
    desc = ""
    deadline = time.time() + wait_sec
    while time.time() < deadline:
        time.sleep(2.0)
        found, responding, desc = window_state()
        if found and responding and "界面已就绪" in read_log():
            break

    elapsed = time.time() - t0
    print(f"  耗时      : {elapsed:.1f}s")
    print(f"  窗口      : {desc if found else '（未找到）'}")

    log_txt = read_log()

    ready = "界面已就绪" in log_txt
    crashed = any(k in log_txt for k in ("access violation", "Windows fatal exception", "Fatal Python error",
        "Traceback (most recent call last)",
    ))

    print(f"  界面就绪  : {'[OK] 是' if ready else '[X] 否'}")
    print(f"  崩溃痕迹  : {'[X] 有' if crashed else '[OK] 无'}")

    ok = found and responding and ready and not crashed
    print(f"  判定      : {' 通过 ' if ok else ' 失败 '}")

    if not ok:
        print("\n  -- 日志尾部 --")
        for ln in log_txt.splitlines()[-15:]:
            print(f"    {ln}")

    kill_all()
    return ok


def main() -> int:
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    wait = float(sys.argv[2]) if len(sys.argv) > 2 else 22.0

    print("=" * 62)
    print("  小组件反复启动测试 -- 确认「未响应」彻底消失")
    print("=" * 62)
    print(f"  轮数     : {rounds}")
    print(f"  每轮等待 : {wait:.0f} 秒")
    print(f"  解释器   : {PYW}")

    if not PYW.exists():
        print(f"\n[X] 找不到 {PYW}")
        return 1

    results = []
    for i in range(1, rounds + 1):
        try:
            results.append(run_one(i, wait))
        except Exception as e:
            print(f"\n  第 {i} 轮异常：{type(e).__name__}: {e}")
            results.append(False)
            kill_all()

    print(f"\n{'=' * 62}")
    passed = sum(results)
    print(f"  结果：{passed}/{len(results)} 轮通过")
    if passed == len(results):
        print("   全部通过 -- 「未响应」已修复 ")
    else:
        print("  [X] 有失败轮次，需要继续排查")
        for i, ok in enumerate(results, 1):
            print(f"      第 {i} 轮：{'通过' if ok else '失败'}")
    print(f"{'=' * 62}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
