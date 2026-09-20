"""Edge 真实浏览器引擎 —— 关键设计。

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
    2. **取数**：需要读数据时，Playwright 通过 CDP **连接**到已存在的
       Edge（或另起一个无头实例），只做页面读取，从不参与登录。

这样做的直接好处：
    * 不需要你再关闭日常使用的 Edge（用的是独立数据目录）
    * 登录环节与人类操作无法区分，不会触发风控
    * 读取环节即使被识别为自动化，也只是 GET 页面，风险极低
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from playwright.sync_api import Browser, Page, Playwright, sync_playwright

from . import config

log = logging.getLogger(__name__)

_HOME = f"{config.BASE_URL}/student/home"

# 判定为"未登录"的 URL 特征
_LOGIN_HINTS = ("/login", "/sign_in", "/signin", "/users/sign_in", "/sso", "saml", "oauth")

_EDGE_CANDIDATES = [
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\EdgeCore\msedge.exe"),
]


# ======================== Edge 进程管理 ========================

def find_edge() -> Path | None:
    """定位 msedge.exe。"""
    for p in _EDGE_CANDIDATES:
        if p.exists():
            return p
    found = shutil.which("msedge")
    return Path(found) if found else None


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
        "--disable-features=msEdgeSidebarV2,msEdgeLaunchOnLogin,msShowFeatureSplash",
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


# ======================== 登录（真人操作） ========================

def open_login_browser() -> subprocess.Popen:
    """打开真实 Edge 供你**手动登录**。

    注意：这里只启动浏览器，**不连接 CDP、不注入脚本**，
    确保登录过程与人类操作完全一致。
    """
    config.ensure_dirs()
    return launch_edge(config.BROWSER_PROFILE_DIR, headless=False, url=_HOME, debug_port=True
    )


def close_browser(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
    except Exception:
        pass


# ======================== 取数会话 ========================

class Session:
    """取数会话：连接到一个真实 Edge 实例，只读页面。

    与之前接口保持一致（open / page / close），方便上层直接调用。
    """

    def __init__(self, headless: bool = True, profile_dir: Path | None = None) -> None:
        self.headless = headless
        self.profile_dir = Path(profile_dir or config.BROWSER_PROFILE_DIR)
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._proc: subprocess.Popen | None = None
        self._owned = False          # 这个 Edge 是否由本会话启动

    # ---------- 启动 ----------

    def open(self) -> "Session":
        config.ensure_dirs()

        if is_edge_running(self.profile_dir):
            log.info("复用已在运行的 Edge 实例")
        else:
            self._proc = launch_edge(self.profile_dir, headless=self.headless, debug_port=True
            )
            self._owned = True

        port = wait_for_devtools(self.profile_dir)
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        return self

    def close(self) -> None:
        # 只断开连接；**绝不去关闭用户的浏览器**
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        self._pw = None
        self._browser = None
        # 仅当我们自己启动的实例才回收
        if self._owned:
            close_browser(self._proc)
        self._proc = None
        self._owned = False

    def __enter__(self) -> "Session":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------- 页面 ----------

    def _context(self):
        if self._browser is None:
            raise RuntimeError("会话尚未打开")
        if self._browser.contexts:
            return self._browser.contexts[0]
        return self._browser.new_context()

    def page(self) -> Page:
        ctx = self._context()
        pages = [p for p in ctx.pages if not p.is_closed()]
        return pages[0] if pages else ctx.new_page()

    # ---------- 登录状态 ----------

    def is_logged_in(self) -> bool:
        return check_logged_in(self.page())


def check_logged_in(page: Page, settle_ms: int = 1500) -> bool:
    """访问学生首页并判断是否已登录。"""
    try:
        page.goto(_HOME, wait_until="domcontentloaded")
        page.wait_for_timeout(settle_ms)
    except Exception as e:
        log.warning("访问首页失败：%s", e)
        return False
    return looks_logged_in(page)


def looks_logged_in(page: Page) -> bool:
    """仅根据当前 URL 与页面内容判断，不发起导航。"""
    try:
        url = (page.url or "").lower()
        if any(h in url for h in _LOGIN_HINTS):
            return False
        if "/student/" not in url:
            return False
        return len(page.inner_text("body").strip()) > 150
    except Exception:
        return False


# ======================== 诊断 ========================

def diagnose() -> dict:
    """检查浏览器环境，用于排查登录问题。"""
    out: dict = {"edge": None, "running": False, "webdriver": None,
                 "title": "", "url": "", "logged_in": False, "error": ""}
    exe = find_edge()
    out["edge"] = str(exe) if exe else None
    if exe is None:
        out["error"] = "未找到 msedge.exe"
        return out

    s = Session(headless=True)
    try:
        s.open()
        out["running"] = True
        page = s.page()
        # 关键：确认自动化指纹为 false
        try:
            out["webdriver"] = page.evaluate("() => navigator.webdriver")
            out["ua"] = page.evaluate("() => navigator.userAgent")
        except Exception:
            pass
        page.goto(_HOME, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        out["title"] = page.title()
        out["url"] = page.url
        out["logged_in"] = looks_logged_in(page)
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    finally:
        s.close()
    return out


def json_dumps(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)
