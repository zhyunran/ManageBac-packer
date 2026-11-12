"""打开网页 —— 带上我们自己的登录态，而不是甩给系统默认浏览器。

要解决的问题
------------
原来点「打开 ManageBac」跳转过去，浏览器里还要再输一遍账号密码。
原因：抓数据用的是组件自己的 profile（``data/browser-profile``），
   而 ``webbrowser.open()`` 用的是系统默认浏览器 —— 两边登录态无关。

现在的做法
----------
用**组件自己的 Edge profile** 打开链接 —— 那里已经登录过，
所以点进去直接就是登录状态。

不干扰你自己开的 Edge
---------------------
用的是独立 profile（``data/browser-profile``），与你的日常 Edge 完全隔离。
Edge 允许多个实例用不同 `--user-data-dir` 同时运行，互不影响。

复用已经在跑的实例
------------------
如果组件自己的 Edge 已经开着（比如刚同步过 Teams），
就直接连上去**新开一个标签**，而不是再起一个进程 ——
否则会出现两个窗口，看着乱，也浪费资源。
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import config, edge

log = logging.getLogger(__name__)


def _cdp_json(port: int, path: str, method: str = "GET",
              timeout: float = 6.0):
    """调一次 CDP 的 HTTP 接口，返回解析后的 JSON（失败返回 None）。"""
    try:
        api = f"http://127.0.0.1:{port}{path}"
        req = urllib.request.Request(api, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
        return json.loads(raw) if raw.strip() else {}
    except Exception as e:
        log.debug("CDP 请求失败（%s %s）：%s", method, path, e)
        return None


def _instance_headless(port: int):
    """这个 CDP 实例是不是**无头**的。返回 True / False / None（判断不了）。

    ★★ 这就是「说打开了、其实什么都没发生」的根源 ★★

      抓取用的 Edge 是 ``--headless`` 起的：它没有窗口。
      点「打开原网页」时我们通过 CDP 往里新开标签 —— 调用确实成功，
      接口如实返回「已打开」，但那个标签在一个**看不见的浏览器**里。
      界面上于是出现「提示打开了，屏幕毫无变化」。

      判据：无头实例的 User-Agent 里带 ``Headless``
      （实测 ``HeadlessChrome/156.0.0.0``，``HeadlessEdg`` 也会出现）。
    """
    v = _cdp_json(port, "/json/version", timeout=4)
    if not isinstance(v, dict):
        return None
    ua = str(v.get("User-Agent") or "")
    browser = str(v.get("Browser") or "")
    if "Headless" in ua or "Headless" in browser:
        return True
    if ua:
        return False
    return None


def _cdp_new_tab(port: int, url: str):
    """通过 CDP 在已运行的浏览器里新开一个标签，返回标签 id（失败 None）。"""
    try:
        path = f"/json/new?{urllib.parse.quote(url, safe='')}"
        info = _cdp_json(port, path, method="PUT", timeout=8)
        if isinstance(info, dict):
            return str(info.get("id") or "ok")
        return None
    except Exception as e:
        log.debug("通过 CDP 新开标签失败：%s", e)
        return None


def _cdp_activate(port: int, target_id: str) -> bool:
    """把某个标签页切到前台（CDP 的 /json/activate）。"""
    if not target_id or target_id == "ok":
        return False
    return _cdp_json(port, f"/json/activate/{target_id}", timeout=5) is not None


def _visible_windows_of(pid: int) -> list:
    """某进程的可见顶层窗口句柄。"""
    try:
        import ctypes  # noqa: PLC0415
        from ctypes import wintypes  # noqa: PLC0415
    except Exception:
        return []
    u32 = ctypes.windll.user32
    found: list = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                     wintypes.LPARAM)  # noqa: N806

    def _cb(hwnd, _lparam):
        try:
            if not u32.IsWindowVisible(hwnd):
                return 1
            wpid = wintypes.DWORD()
            u32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
            if wpid.value == pid and u32.GetWindowTextLengthW(hwnd) > 0:
                found.append(int(hwnd))
        except Exception:
            pass
        return 1

    try:
        u32.EnumWindows(WNDENUMPROC(_cb), 0)
    except Exception:
        return []
    return found


def _focus_browser_window(profile_dir, pid_hint: int = 0,
                          tries: int = 10) -> bool:
    """把本 profile 的浏览器窗口叫到最前面（最小化的先还原）。

    【为什么必须做】
      标签页开出来还不够 —— 窗口可能最小化了、或被别的窗口压在后面。
      用户判断「有没有打开」只看屏幕，看不见就等于没打开。

    只认本 profile 的进程（cdp.list_profile_pids 按命令行匹配），
    用户日常使用的 Edge 不会被碰。
    """
    try:
        import ctypes  # noqa: PLC0415
        from ctypes import wintypes  # noqa: PLC0415
    except Exception:
        return False

    u32 = ctypes.windll.user32
    SW_RESTORE = 9  # noqa: N806
    last_err = ""

    for attempt in range(max(1, tries)):
        pids: list = []
        if pid_hint:
            pids.append(pid_hint)
        try:
            from app import cdp  # noqa: PLC0415
            for x in cdp.list_profile_pids(profile_dir):
                if x not in pids:
                    pids.append(x)
        except Exception as e:
            last_err = str(e)

        for pid in pids:
            for hwnd in _visible_windows_of(pid):
                try:
                    u32.ShowWindow(wintypes.HWND(hwnd), SW_RESTORE)
                    u32.BringWindowToTop(wintypes.HWND(hwnd))
                    u32.SetForegroundWindow(wintypes.HWND(hwnd))
                    log.info("已把浏览器窗口叫到前面（pid=%s）", pid)
                    return True
                except Exception as e:
                    last_err = str(e)
        if attempt < tries - 1:
            time.sleep(0.4)          # 刚启动时窗口还没建出来
    log.debug("没能把浏览器窗口叫到前面：%s", last_err or "没找到可见窗口")
    return False


#  上面用到了 urllib.parse，单独 import 一下（放在这里避免顶部堆 import）
import urllib.parse  # noqa: E402


def open_in_own_profile(url: str, reuse: bool = True) -> dict:
    """用组件自己的 Edge profile 打开链接。

    返回 ``{ok, how}``：
      - ``how='reused'``  复用已在跑的 Edge，新开标签
      - ``how='launched'`` 新起了一个 Edge 进程
      - ``how='no_edge'``  这台机器上没找到 Edge（调用方据此如实提示用户）
      - ``ok=False``       其它失败（调用方可以退回系统浏览器）

    reuse=False 时总是新起进程（用于「打开 Teams」这种希望有独立窗口的场景）。
    """
    if not url:
        return {"ok": False, "how": "empty"}

    prof = config.BROWSER_PROFILE_DIR
    config.ensure_dirs()

    #  ⓪ 先确认 Edge 在不在。不在就别装模作样往下走 ——
    #    明确回报「没有 Edge」，让界面能如实告诉用户
    #    「已改用系统默认浏览器打开，需要重新登录」，
    #    而不是静默降级、让用户以为「怎么又要我登录一次」。
    if edge.find_edge() is None:
        log.warning("没找到 Microsoft Edge，无法用自带登录态打开：%s", url[:120])
        return {"ok": False, "how": "no_edge",
                "error": "没找到 Microsoft Edge"}

    # ① 已经在跑？先看它是不是**无头**实例
    if reuse:
        port = edge.read_devtools_port(prof)
        if port and edge.is_edge_running(prof):
            headless = _instance_headless(port)
            if headless is True:
                #  ★ 关键修复 ——
                #    抓取用的 Edge 是 --headless 起的，没有窗口。
                #    往它里面开标签，接口会如实返回成功，但标签在
                #    一个看不见的浏览器里：用户只看到「说打开了、屏幕没反应」。
                #    同一个 user-data-dir 不能同时存在两个实例（Chromium 单实例锁），
                #    所以必须先把无头实例收掉，再起一个可见窗口。
                log.info("已在跑的 Edge 是无头实例（没有窗口），先收掉，"
                         "改起一个可见窗口 —— 否则标签开在看不见的浏览器里")
                try:
                    from app import cdp  # noqa: PLC0415
                    cdp.close_profile_browser(prof, timeout=12)
                except Exception as e:
                    log.warning("收掉无头 Edge 失败（继续尝试启动）：%s", e)
            else:
                tid = _cdp_new_tab(port, url)
                if tid:
                    _cdp_activate(port, tid)
                    #  窗口可能最小化或被压在后面 —— 必须叫到前面，
                    #  否则用户看到的现象跟「没打开」一样。
                    focused = _focus_browser_window(prof)
                    log.info("在已开窗口里新开标签：%s（已前置=%s）",
                             url[:120], focused)
                    return {"ok": True, "how": "reused", "port": port,
                            "focused": focused}
                log.warning("复用已有窗口失败，改为新开一个窗口")

    # ② 没在跑（或刚收掉无头实例）→ 起一个可见窗口，带上我们的 profile
    try:
        p = edge.launch_edge(prof, headless=False, url=url, debug_port=True)
        pid = int(getattr(p, "pid", 0) or 0)
        focused = _focus_browser_window(prof, pid_hint=pid)
        log.info("启动 Edge（自带登录态，可见窗口）打开：%s（已前置=%s）",
                 url[:120], focused)
        return {"ok": True, "how": "launched", "focused": focused}
    except Exception as e:
        log.warning("用自带 profile 打开失败：%s", e)
        return {"ok": False, "how": "error", "error": str(e)}


def is_own_edge_running() -> bool:
    """组件自己的 Edge 现在开着吗。"""
    prof = config.BROWSER_PROFILE_DIR
    port = edge.read_devtools_port(prof)
    return bool(port) and edge.is_edge_running(prof)
