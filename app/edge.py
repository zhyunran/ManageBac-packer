"""真实浏览器引擎（任意 Chromium 内核：Edge / Chrome / Brave / Chromium）。

【为什么不用 Playwright 启动浏览器】
    Playwright 启动的浏览器带自动化特征（``--enable-automation``、
    ``navigator.webdriver=true``、CDP 附加等）。ManageBac / Faria 的风控会
    判定为机器人，导致**密码明明正确却提示错误** —— 这不是密码问题，
    而是登录请求在风控层就被拒绝了。

【本模块的做法】
    1. **登录**：用 ``subprocess`` 启动一个**货真价实的 Edge 进程**
       （与手动双击打开完全一致，只是换了独立的用户数据目录），
       没有任何自动化开关。你在里面正常输入账号密码 —— 对网站来说
       这 100% 是一次普通的人类登录。
       **登录过程中绝不连接 CDP。**
    2. **取数**：需要读数据时，由 ``app/cdp.py`` 通过 CDP **连接**到已存在的
       Edge（或另起一个无头实例），只做页面读取，从不参与登录。

    （历史说明：取数会话曾经也住在这里、走 Playwright；v0.5 已整体迁到
    ``app/cdp.py`` 的纯 WebSocket CDP 实现，Playwright 依赖随之移除。
    本模块现在只剩一件事 —— 启动/探测真实的 Edge 进程。）

这样做的直接好处：
    * 不需要你再关闭日常使用的 Edge（用的是独立数据目录）
    * 登录环节与人类操作无法区分，不会触发风控
    * 读取环节即使被识别为自动化，也只是 GET 页面，风险极低
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

#  ★ 浏览器定位统一在 app/browser.py（任意 Chromium 内核浏览器都可）。
#    以前只有三行硬编码，落空时 find_edge() 返回 None，
#    于是「点击跳转原网页」静默退回系统默认浏览器（要重新登录一次），
#    而 Teams / EC 名单 / 附件下载会**一起失效**且界面上没有任何提示。
def find_edge() -> Path | None:
    """定位可用的 Chromium 浏览器（不再写死 msedge.exe）。

    见 app/browser.py：自动挑系统默认浏览器，也可用 config.BROWSER 指定。
    """
    from . import browser as _browser
    return _browser.find()


def find_webview2() -> Path | None:
    """定位 WebView2 运行时（界面渲染用的 msedgewebview2.exe）。

    ★ WebView2 与 Edge 浏览器是**两个独立组件**：企业机器上完全可能
      「有 WebView2 没有 Edge」—— 此时界面照常打开、不报任何错，
      但跳转与 Teams/EC 全部失灵。所以要分开检测。
    """
    for base in (os.environ.get("PROGRAMFILES(X86)"),
                 os.environ.get("PROGRAMFILES"),
                 os.environ.get("LOCALAPPDATA")):
        if not base:
            continue
        p = (Path(base) / "Microsoft" / "EdgeWebView" / "Application"
             / "msedgewebview2.exe")
        try:
            if p.is_file():
                return p
        except OSError:
            continue
    #  注册表登记的 WebView2 版本目录（pv 值）
    try:
        import winreg
        for root, sub in (
            (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients"
             r"\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
            (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\Microsoft\EdgeUpdate\Clients"
             r"\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
            (winreg.HKEY_CURRENT_USER,
             r"SOFTWARE\Microsoft\EdgeUpdate\Clients"
             r"\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
        ):
            try:
                with winreg.OpenKey(root, sub) as k:
                    pv, _ = winreg.QueryValueEx(k, "pv")
                if pv:
                    for base in (os.environ.get("PROGRAMFILES(X86)"),
                                 os.environ.get("PROGRAMFILES")):
                        if not base:
                            continue
                        p = (Path(base) / "Microsoft" / "EdgeWebView"
                             / "Application" / str(pv) / "msedgewebview2.exe")
                        if p.is_file():
                            return p
            except OSError:
                continue
    except Exception as e:
        log.debug("读注册表找 WebView2 失败：%s", e)
    return None


def browser_health(profile_dir: Path | None = None) -> dict:
    """浏览器依赖体检 —— 缺什么就如实说，不静默降级。

    返回 {ok, edge, edge_path, webview2, webview2_path, profile, profile_ready,
          missing:[...], message}
    """
    try:
        from . import config
        prof = Path(profile_dir or config.BROWSER_PROFILE_DIR)
    except Exception:
        prof = Path(profile_dir) if profile_dir else None

    exe = find_edge()
    wv2 = find_webview2()
    prof_ready = bool(prof and prof.is_dir())

    #  ★ 只有「真的缺组件」才算不健康。
    #    独立配置目录是**首次启动时自动创建**的，全新安装上它本来就不存在 ——
    #    把它算进 missing 会让每个新用户开机就看到一条假告警。
    #    （实测踩到：源码模式下 profile 不存在 → 误报。）
    missing: list[str] = []
    if exe is None:
        missing.append("browser")
    if wv2 is None:
        missing.append("webview2")

    from . import browser as _browser
    bname = _browser.display_name(exe)

    if not missing:
        msg = f"浏览器依赖正常（{bname}）"
    elif "browser" in missing:
        #  缺浏览器的影响远不止「跳转」——必须说清楚，且不再点名 Edge
        msg = ("没找到任何 Chromium 内核浏览器（Edge / Chrome / Brave / Chromium）："
               "点击跳转、Teams、EC 名单、附件下载都会不可用（界面仍可打开）。"
               "装其中任意一个即可，也可用 config.BROWSER / 环境变量 MBPK_BROWSER 指定")
    else:
        msg = "没找到 WebView2 运行时装（界面渲染需要它）"

    return {
        "ok": not missing,
        #  兼容旧键名（外部脚本可能在用），但语义已是「任意 Chromium 浏览器」
        "edge": exe is not None,
        "edge_path": str(exe) if exe else "",
        "browser": exe is not None,
        "browser_name": bname,
        "browser_path": str(exe) if exe else "",
        "webview2": wv2 is not None,
        "webview2_path": str(wv2) if wv2 else "",
        "profile": str(prof) if prof else "",
        #  仅作诊断：False 不代表有问题（首次运行前就是 False）
        "profile_ready": prof_ready,
        "missing": missing,
        "message": msg,
    }


# ======================== Edge 进程管理 ========================

def _port_file(profile_dir: Path) -> Path:
    return profile_dir / "DevToolsActivePort"


def read_devtools_port(profile_dir: Path) -> int | None:
    """Edge 启动后会把自己的调试端口写入 DevToolsActivePort。"""
    f = _port_file(profile_dir)
    if not f.exists():
        return None
    try:
        first = f.read_text(encoding="utf-8", errors="ignore").splitlines()[0]
        return int(first.strip())
    except (IndexError, ValueError, OSError):
        return None


def is_edge_running(profile_dir: Path) -> bool:
    """判断该数据目录下是否已有可连接的 Edge 实例。"""
    port = read_devtools_port(profile_dir)
    if not port:
        return False
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2
        ) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def launch_edge(profile_dir: Path,
    headless: bool = False,
    url: str | None = None,
    debug_port: bool = True,
) -> subprocess.Popen:
    """启动一个**真实的** Edge 进程（无任何自动化开关）。

    debug_port 打开只是为了稍后能读取数据；它本身不是自动化特征，
    也不会触发 navigator.webdriver。
    """
    exe = find_edge()
    if exe is None:
        raise FileNotFoundError("未找到 msedge.exe，请确认已安装 Microsoft Edge")

    profile_dir.mkdir(parents=True, exist_ok=True)

    #  ★ 启动前先把「正在退出但没退完」的旧实例送走完。
    #    踩过的坑：上一批操作（如全量收取）的 Edge 正在退出时立刻
    #    起新实例 —— 新进程看见同 profile 的单实例锁还在旧进程手里，
    #    会把启动参数转手递过去然后自己退出，调试端口文件永远
    #    写不出来 → 「等待 Edge 调试端口超时」。
    #    close_profile_browser 只认本 profile 的命令行，
    #    用户日常在用的 Edge 不受影响。
    if not is_edge_running(profile_dir):
        try:
            from app import cdp
            if cdp.list_profile_pids(profile_dir):
                log.info("旧 Edge 实例还没退完，先收掉再起新的")
                cdp.close_profile_browser(profile_dir)
        except Exception as e:
            log.debug("清旧实例失败（不挡启动）：%s", e)

    # 清掉上一次可能残留的端口文件，避免误判
    try:
        _port_file(profile_dir).unlink(missing_ok=True)
    except OSError:
        pass

    flags: list[str] = [
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        # 避免登录了微软账号后跳出多余的引导
        # （EdgeOpenExternalProtocolPrompt：不再询问「是否用外部应用打开」）
        "--disable-features=msEdgeSidebarV2,msEdgeLaunchOnLogin,"
        "msShowFeatureSplash,msEdgeAppLauncher,"
        "EdgeOpenExternalProtocolPrompt",
    ]
    if debug_port:
        flags.append("--remote-debugging-port=0")
    if headless:
        flags += [
            "--headless=new",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-background-timer-throttling",
        ]
    if url:
        flags.append(url)

    log.info("启动 Edge：%s（headless=%s, debug=%s）", exe, headless, debug_port)
    return subprocess.Popen([str(exe), *flags],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


def wait_for_devtools(profile_dir: Path, timeout: float = 45.0) -> int:
    """等待调试端口就绪，返回端口号。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_edge_running(profile_dir):
            port = read_devtools_port(profile_dir)
            if port:
                return port
        time.sleep(0.4)
    raise TimeoutError("等待 Edge 调试端口超时")
