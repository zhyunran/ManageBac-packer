"""无窗口地跑外部命令（0.5.2）。

【为什么需要这个模块】
    0.5.1 及以前，项目里的 PowerShell / taskkill 调用都是这样写的：

        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, text=True, timeout=30)

    **这会弹出一个控制台黑框。** 用户看到的是「狂跳 C:\\Windows\\System32
    的输入框，长得像 PowerShell」—— 每次点开 Teams、每次登录、每次关浏览器，
    都要跑好几轮 PowerShell（循环里每 0.5 秒一次），于是黑框一直在闪。

    全项目只有 `stress_start.py` 一处加了 `creationflags`，
    其它地方（cdp / edge / widget / make_startup / cold_start_check）
    全都漏了。

【做法】
    Windows 上有两个开关，效果一样，都要用：
        CREATE_NO_WINDOW      子进程完全不建控制台
        STARTF_USESHOWWINDOW
        + SW_HIDE             （给显示方式兜底）

    统一走 `run()` / `popen()`，不要在别处直接写 subprocess。

【注意】
    非 Windows 平台（如果将来要跑）这两个常量不存在，
    所以下面做了兼容判断，取不到就不传。
"""
from __future__ import annotations

import subprocess
import sys
from typing import Any

_IS_WIN = sys.platform == "win32"

#  CREATE_NO_WINDOW = 0x08000000
#  用一个常量把它与 STARTUPINFO 打包，避免每个调用点都写一遍
_CREATE_NO_WINDOW = 0x08000000 if _IS_WIN else 0


def _startupinfo():
    """给子进程一个「隐藏窗口」的启动信息（CREATE_NO_WINDOW 的兜底）。"""
    if not _IS_WIN:
        return None
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0          # SW_HIDE
        return si
    except Exception:
        return None


def _flags() -> int:
    return _CREATE_NO_WINDOW


def run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess:
    """`subprocess.run` 的无窗口版本。

    参数与标准库一致；**不要**在调用方再传 creationflags / startupinfo
    （这里已经填好了）。
    """
    kw.setdefault("creationflags", _flags())
    si = _startupinfo()
    if si is not None:
        kw.setdefault("startupinfo", si)
    #  有些调用点会传 text=True / capture_output=True，原样透传
    return subprocess.run(cmd, **kw)


def popen(cmd: list[str], **kw: Any) -> subprocess.Popen:
    """`subprocess.Popen` 的无窗口版本。"""
    kw.setdefault("creationflags", _flags())
    si = _startupinfo()
    if si is not None:
        kw.setdefault("startupinfo", si)
    return subprocess.Popen(cmd, **kw)


def check(cmd: list[str], **kw: Any) -> int:
    """跑一条命令、只要退出码（不要输出）。"""
    kw.setdefault("stdout", subprocess.DEVNULL)
    kw.setdefault("stderr", subprocess.DEVNULL)
    kw.setdefault("stdin", subprocess.DEVNULL)
    try:
        return run(cmd, **kw).returncode
    except Exception:
        return -1


def kill_pid(pid: int, force: bool = True) -> bool:
    """结束一个进程（无窗口）。

    ★ 不用 `taskkill`：它是外部程序，每次调用都要 spawn 一个新进程 ——
      在「主进程半死、还要杀掉自己父进程」的场景里会卡住不返回。
      这里直接用 ctypes 调 Win32 API，纯进程内、不 spawn、不可能卡。

    （这条教训来自本项目 0.5.0 的看门狗：`taskkill` 曾让看门狗永久挂住。）
    """
    if not _IS_WIN:
        try:
            import os
            import signal

            os.kill(pid, signal.SIGTERM)
            return True
        except Exception:
            return False

    import ctypes
    from ctypes import wintypes

    try:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        #  签名必须写全 —— 不写 restype 时 ctypes 默认按 32 位 int 取，
        #  HANDLE（64 位）会被截断（踩过）
        k.OpenProcess.restype = wintypes.HANDLE
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.TerminateProcess.restype = wintypes.BOOL
        k.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k.CloseHandle.restype = wintypes.BOOL
        k.CloseHandle.argtypes = [wintypes.HANDLE]

        PROCESS_TERMINATE = 0x0001
        h = k.OpenProcess(PROCESS_TERMINATE, False, int(pid))
        if not h:
            return False
        try:
            return bool(k.TerminateProcess(h, 1))
        finally:
            k.CloseHandle(h)
    except Exception:
        return False
