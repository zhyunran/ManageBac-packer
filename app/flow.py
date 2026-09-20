"""两段式取数流程 —— 核心设计。

【为什么要分两段】
    实测证明：只要加上 ``--remote-debugging-port`` 参数，
    ``navigator.webdriver`` 就会变为 ``true``，ManageBac 的风控因此
    在密码校验前就拒绝登录（表现为「密码正确却提示错误」）。

    但程序又必须依赖调试端口才能读取页面内容（浏览器的安全边界）。

    解决办法：把「登录」与「读取」彻底分开。

        第 1 段：干净 Edge（无调试端口）→ 你手动登录
        第 2 段：同一数据目录启动（带调试端口）→ 已登录，直接读取

    第 2 段不需要登录，所以 webdriver=true 不会触发任何风控。

【一个必须处理的细节：Edge 是单例的】
    同一个数据目录只允许一个 Edge 实例。若第 1 段的窗口还开着，
    第 2 段带端口启动会被"接管"后立即退出，端口永远建不起来。
    因此切换阶段前必须**先关闭**本数据目录的进程
    （只关本程序的 profile，你日常的 Edge 完全不受影响）。
"""
from __future__ import annotations

import logging
import shutil
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path

from . import cdp, config

log = logging.getLogger(__name__)

HOME = f"{config.BASE_URL}/student/home"


# ==================== 登录状态检测（读取 Cookie 数据库） ====================

def _cookie_db_candidates() -> list[Path]:
    base = config.BROWSER_PROFILE_DIR
    return [
        base / "Default" / "Network" / "Cookies",
        base / "Default" / "Cookies",
        base / "Cookies",
    ]


def read_cookie_names(host_fragment: str = "managebac") -> list[str]:
    """读取（副本）Cookie 数据库里属于该站点的 Cookie 名。

    **只读取名字，不解密内容** —— 目的是判断是否已登录。
    浏览器运行时会锁定数据库，因此先复制一份副本再读。
    """
    src = next((p for p in _cookie_db_candidates() if p.exists()), None)
    if src is None:
        return []

    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tf:
            tmp_path = tf.name
        shutil.copy2(src, tmp_path)          # 锁定时可能失败，交由 except 处理
        conn = sqlite3.connect(f"file:{tmp_path}?mode=ro", uri=True)
        try:
            rows = conn.execute("SELECT name FROM cookies WHERE host_key LIKE ?",
                (f"%{host_fragment}%",),
            ).fetchall()
            return [r[0] for r in rows]
        finally:
            conn.close()
    except Exception as e:
        log.debug("读取 Cookie 数据库失败：%s", e)
        return []
    finally:
        if tmp_path:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except OSError:
                pass


# 判定"已登录"的 Cookie 名特征（Rails 系应用通常是 _session 之类）
_SESSION_HINTS = ("session", "remember", "auth", "token", "user")


def has_login_cookie() -> bool:
    """根据 Cookie 名判断是否已登录。"""
    names = [n.lower() for n in read_cookie_names()]
    if not names:
        return False
    return any(any(h in n for h in _SESSION_HINTS) for n in names)


# ==================== 进程管理 ====================

def stop_all(wait: float = 20.0) -> int:
    """关闭本数据目录的所有 Edge 进程（不影响你日常使用的 Edge）。"""
    try:
        n = cdp.close_profile_browser(config.BROWSER_PROFILE_DIR, timeout=wait)
        log.info("已关闭本数据目录的 %d 个 Edge 进程", n)
        return n
    except Exception as e:
        log.warning("关闭浏览器时出错：%s", e)
        return 0


def is_open() -> bool:
    return bool(cdp.list_profile_pids(config.BROWSER_PROFILE_DIR))


# ==================== 第 1 段：干净登录 ====================

def launch_clean_browser(url: str | None = None) -> subprocess.Popen:
    """启动**不带调试端口**的 Edge —— 与手动双击打开完全一致。

    该环境下 ``navigator.webdriver == false``，登录绝不触发风控。
    代价是程序读不到页面内容，因此只用于登录阶段。
    """
    config.ensure_dirs()
    exe = cdp.find_edge()
    if exe is None:
        raise FileNotFoundError("未找到 msedge.exe，请确认已安装 Microsoft Edge")

    flags = [
        f"--user-data-dir={config.BROWSER_PROFILE_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=msEdgeSidebarV2,msEdgeLaunchOnLogin,msShowFeatureSplash",
    ]
    if url:
        flags.append(url)

    log.info("启动干净 Edge（无调试端口）")
    return subprocess.Popen([str(exe), *flags],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
    )


def wait_for_login(timeout: float = 600.0, on_tick=None,
                   require_close: bool = False) -> bool:
    """等待你完成登录。

    判断依据是 Cookie 数据库里出现了会话 Cookie，因此
    **完全不需要连上浏览器**，也不会干扰你正在操作的窗口。

    require_close=True 时，以"浏览器已关闭"作为完成信号（最后兜底）。
    """
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(3)
        if has_login_cookie():
            return True
        if require_close and not is_open():
            time.sleep(1.0)
            return has_login_cookie()
        if on_tick:
            try:
                on_tick(int(time.time() - t0))
            except Exception:
                pass
    return False


# ==================== 第 2 段：带端口读取 ====================

def open_reader(headless: bool = True, timeout: float = 50.0) -> cdp.Session:
    """切换到「可读取」状态：关闭登录窗口，用同一数据目录启动带端口的实例。

    此时已登录，无需再登录，所以不会触发风控。
    """
    # 单例限制：必须先关掉干净窗口，否则端口建不起来
    if is_open():
        stop_all()

    cdp.launch(config.BROWSER_PROFILE_DIR, headless=headless, url=HOME)
    port = cdp.wait_for_port(config.BROWSER_PROFILE_DIR, timeout=timeout)
    time.sleep(1.5)

    session = cdp.Session(headless=headless)
    session.open()
    return session


# ==================== 组合流程 ====================

def check_access() -> tuple[bool, str]:
    """快速检查是否已具备读取条件（不启动任何浏览器）。"""
    if not config.BROWSER_PROFILE_DIR.exists():
        return False, "尚未登录过"
    if not has_login_cookie():
        return False, "未找到登录 Cookie"
    return True, "已登录"


def ensure_login(verbose: bool = True) -> tuple[bool, str]:
    """确保已登录。未登录则打开干净窗口引导你完成一次登录。"""
    ok, msg = check_access()
    if ok:
        return True, msg

    config.ensure_dirs()

    if verbose:
        print("=" * 72)
        print("  需要先登录一次")
        print("=" * 72)
        print("  即将打开一个 Edge 窗口。")
        print("  这个窗口【不带调试端口】，不会被风控识别，登录方式与平时完全一样。")
        print()

    if not is_open():
        launch_clean_browser(HOME)
    else:
        if verbose:
            print("  [i] 检测到该窗口已打开，请直接在其中登录。")

    if verbose:
        print("  请在其中输入账号密码登录。完成后**关闭该窗口**（或等程序自动检测）。")
        print()

    ok = wait_for_login(timeout=600)
    if not ok:
        return False, "未检测到登录"

    # 切换阶段前先关掉干净窗口（Edge 单例限制）
    stop_all()
    return True, "登录成功"


def shutdown() -> None:
    """收尾：关闭本数据目录的 Edge。"""
    stop_all()
