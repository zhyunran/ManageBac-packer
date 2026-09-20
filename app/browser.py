"""浏览器会话：组件自带一个浏览器窗口，用于手动登录与取数。

设计要点：
- 使用**独立**的 user-data-dir（``data/browser-profile``），与你日常 Edge 完全隔离，
  因此**运行时不需要关闭你的 Edge**。
- 登录是**真人手动操作**：程序只负责打开窗口、检测登录成功、保存登录态。
  程序不接触、不猜解、不解密任何密码或 Cookie。
- 登录态由浏览器自身持久化，下次直接复用；过期时重新弹窗让你登录即可。
"""
from __future__ import annotations

import logging
from contextlib import contextmanager

from playwright.sync_api import BrowserContext, Page, Playwright, sync_playwright

from . import config

log = logging.getLogger(__name__)

# 判定为"未登录"的 URL 特征
_LOGIN_HINTS = ("/login", "/sign_in", "/signin", "/users/sign_in", "/sso", "saml", "oauth")


def _launch_args() -> list[str]:
    return [
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=msEdgeSidebarV2,msEdgeLaunchOnLogin",
    ]


def check_logged_in(page: Page, settle_ms: int = 1200) -> bool:
    """访问学生首页并判断是否已登录。"""
    try:
        page.goto(f"{config.BASE_URL}/student/home", wait_until="domcontentloaded")
        page.wait_for_timeout(settle_ms)
    except Exception as e:
        log.warning("访问首页失败：%s", e)
        return False
    return looks_logged_in(page)


def looks_logged_in(page: Page) -> bool:
    """不发起导航，仅根据当前 URL 与页面内容判断是否已登录。"""
    try:
        url = (page.url or "").lower()
        if any(h in url for h in _LOGIN_HINTS):
            return False
        if "/student/" not in url:
            return False
        return len(page.inner_text("body").strip()) > 150
    except Exception:
        return False


class Session:
    """一个浏览器会话（Playwright persistent context）。"""

    def __init__(self, headless: bool = True) -> None:
        self.headless = headless
        self._pw: Playwright | None = None
        self._ctx: BrowserContext | None = None

    # ---------- 生命周期 ----------

    def open(self) -> BrowserContext:
        config.ensure_dirs()
        self._pw = sync_playwright().start()
        self._ctx = self._pw.chromium.launch_persistent_context(user_data_dir=str(config.BROWSER_PROFILE_DIR),
            channel="msedge",
            headless=self.headless,
            args=_launch_args(),
        )
        self._ctx.set_default_timeout(config.PAGE_TIMEOUT_MS)
        self._ctx.set_default_navigation_timeout(config.NAV_TIMEOUT_MS)
        return self._ctx

    def close(self) -> None:
        try:
            if self._ctx is not None:
                self._ctx.close()
        except Exception:
            pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        self._ctx = None
        self._pw = None

    def __enter__(self) -> "Session":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def ctx(self) -> BrowserContext:
        if self._ctx is None:
            raise RuntimeError("会话尚未打开")
        return self._ctx

    def page(self) -> Page:
        """复用一个标签页，避免开出一堆窗口。"""
        pages = [p for p in self.ctx.pages if not p.is_closed()]
        return pages[0] if pages else self.ctx.new_page()

    # ---------- 登录状态 ----------

    def is_logged_in(self, page: Page | None = None) -> bool:
        return check_logged_in(page or self.page())


@contextmanager
def fetch_session():
    """取数用的短生命周期无头会话。"""
    s = Session(headless=True)
    try:
        s.open()
        yield s
    finally:
        s.close()


def open_login_window() -> tuple[Playwright, BrowserContext, Page]:
    """打开**有头**窗口供用户手动登录。调用方负责轮询与关闭。"""
    config.ensure_dirs()
    pw = sync_playwright().start()
    ctx = pw.chromium.launch_persistent_context(user_data_dir=str(config.BROWSER_PROFILE_DIR),
        channel="msedge",
        headless=False,
        args=_launch_args(),
    )
    ctx.set_default_timeout(config.PAGE_TIMEOUT_MS)
    ctx.set_default_navigation_timeout(config.NAV_TIMEOUT_MS)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(f"{config.BASE_URL}/student/home", wait_until="domcontentloaded")
    return pw, ctx, page


def close_login_window(pw: Playwright, ctx: BrowserContext) -> None:
    try:
        ctx.close()
    except Exception:
        pass
    try:
        pw.stop()
    except Exception:
        pass
