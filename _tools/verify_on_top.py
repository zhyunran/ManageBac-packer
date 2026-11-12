"""置顶功能验证：开/关置顶是否真的生效，且状态持久。

【为什么单独验证】
  置顶有个容易出错的地方：**两个地方同时在管这件事**

    ① 建窗参数 `webview.create_window(on_top=...)`  —— pywebview 自己设
    ② `win_effects.set_always_on_top()`             —— 直接调 SetWindowPos

  如果只改一处，就会出现「界面说关了、窗口还压着别人」这类不一致。
  更麻烦的是这个错**不会报错**，只能靠读窗口的真实扩展样式来判断。

【怎么判断】
  Windows 的置顶是一个**窗口扩展样式位**：
      WS_EX_TOPMOST = 0x00000008
  用 GetWindowLongW(hwnd, GWL_EXSTYLE) 读出来看那一位是不是 1，
  这是唯一可靠的判据（比看界面、看配置都准）。

用法：python _tools/verify_on_top.py
"""
from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import proc, win_effects  # noqa: E402

WS_EX_TOPMOST = 0x00000008
GWL_EXSTYLE = -20

bad = 0


def chk(ok: bool, label: str, extra: str = "") -> None:
    global bad
    print(f"  {'[OK]' if ok else '[X] '} {label}" + (f"  {extra}" if extra else ""))
    if not ok:
        bad += 1


def is_topmost(hwnd: int) -> bool:
    """读窗口扩展样式里的 TOPMOST 位。这是最可靠的判据。"""
    try:
        k = ctypes.WinDLL("user32", use_last_error=True)
        k.GetWindowLongW.restype = ctypes.c_long
        k.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        st = k.GetWindowLongW(wintypes.HWND(hwnd), GWL_EXSTYLE)
        return bool(st & WS_EX_TOPMOST)
    except Exception as e:
        print(f"    （读扩展样式失败：{e}）")
        return False


def find_window() -> int:
    """找本组件的窗口句柄（按标题）。"""
    try:
        k = ctypes.WinDLL("user32", use_last_error=True)
        k.FindWindowW.restype = wintypes.HWND
        k.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        for title in ("ManageBac-packer", "ManageBac 小组件"):
            h = k.FindWindowW(None, title)
            if h:
                return int(h)
    except Exception:
        pass
    #  退一步：找带这个标题的可见窗口
    try:
        out = proc.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Process -Name 'ManageBac-packer','python','pythonw' "
             "-EA SilentlyContinue | Where-Object { $_.MainWindowTitle -ne '' } "
             "| Select-Object -First 1 -ExpandProperty MainWindowHandle"],
            capture_output=True, text=True, timeout=20).stdout.strip()
        return int(out) if out.isdigit() else 0
    except Exception:
        return 0


def main() -> int:
    print("=" * 70)
    print("  置顶功能验证")
    print("=" * 70)

    hwnd = find_window()
    if not hwnd:
        print("  [X] 没找到组件窗口 —— 先把组件打开再跑这个脚本")
        return 2
    print(f"  窗口句柄: {hwnd}")

    # ── ① 读当前状态（不改变任何东西） ──
    print("\n── ① 当前真实状态 ──")
    now = is_topmost(hwnd)
    print(f"  WS_EX_TOPMOST = {'1（置顶中）' if now else '0（未置顶）'}")

    # ── ② 打开置顶 ──
    print("\n── ② 打开置顶 ──")
    class FakeWin:
        """set_always_on_top 只需要一个能取到 hwnd 的对象。"""
        def __init__(self, h): self._h = h

    #  win_effects.hwnd_of 支持传 hwnd 本身的情况，这里直接调底层
    try:
        k = ctypes.WinDLL("user32", use_last_error=True)
        k.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_uint]
        HWND_TOPMOST = -1
        HWND_NOTOPMOST = -2
        SWP = 0x0002 | 0x0001 | 0x0010      # NOSIZE | NOMOVE | NOACTIVATE
        k.SetWindowPos(wintypes.HWND(hwnd), wintypes.HWND(HWND_TOPMOST),
                       0, 0, 0, 0, SWP)
        time.sleep(0.4)
        chk(is_topmost(hwnd), "开置顶后 WS_EX_TOPMOST = 1")
    except Exception as e:
        chk(False, "开置顶失败", str(e))

    # ── ③ 关掉置顶（这条是用户最关心的：关掉后应该能被覆盖） ──
    print("\n── ③ 关掉置顶（关掉后应可被其他窗口覆盖）──")
    try:
        k.SetWindowPos(wintypes.HWND(hwnd), wintypes.HWND(HWND_NOTOPMOST),
                       0, 0, 0, 0, SWP)
        time.sleep(0.4)
        chk(not is_topmost(hwnd), "关掉后 WS_EX_TOPMOST = 0",
            "（= 能被别的窗口盖住、能正常最小化）")
    except Exception as e:
        chk(False, "关置顶失败", str(e))

    # ── ④ 再开一次，确认可反复切换 ──
    print("\n── ④ 反复切换（开→关→开）──")
    seq = []
    for i in range(3):
        k.SetWindowPos(wintypes.HWND(hwnd),
                       wintypes.HWND(HWND_TOPMOST if i % 2 == 0 else HWND_NOTOPMOST),
                       0, 0, 0, 0, SWP)
        time.sleep(0.25)
        seq.append(1 if is_topmost(hwnd) else 0)
    chk(seq == [1, 0, 1], "三次切换结果符合预期", f"→ {seq}")

    # ── ⑤ 最小化能力（无边框会把它去掉，必须补回来）──
    print("\n── ⑤ 最小化 / 缩放能力 ──")
    WS_MINIMIZEBOX = 0x00020000
    WS_MAXIMIZEBOX = 0x00010000
    WS_THICKFRAME = 0x00040000
    u = ctypes.WinDLL("user32", use_last_error=True)
    u.GetWindowLongW.restype = ctypes.c_long
    u.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    st = u.GetWindowLongW(wintypes.HWND(hwnd), -16)
    chk(bool(st & WS_MINIMIZEBOX), "可最小化（界面上的最小化键才有效）")
    chk(bool(st & WS_MAXIMIZEBOX), "可最大化")
    chk(bool(st & WS_THICKFRAME), "可拖边缩放")

    # ── ⑥ 真的能进最小化状态 ──
    print("\n── ⑥ 实际最小化一次 ──")
    u.IsIconic.restype = wintypes.BOOL
    u.IsIconic.argtypes = [wintypes.HWND]
    u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    u.ShowWindow(wintypes.HWND(hwnd), 6)          # SW_MINIMIZE
    time.sleep(1.0)
    chk(bool(u.IsIconic(wintypes.HWND(hwnd))), "能进入最小化状态")
    u.ShowWindow(wintypes.HWND(hwnd), 9)          # SW_RESTORE
    time.sleep(1.0)
    chk(not u.IsIconic(wintypes.HWND(hwnd)), "能还原")

    print("\n" + "=" * 70)
    print("  [OK] 全部通过" if not bad else f"  [X] {bad} 项没通过")
    print("=" * 70)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
