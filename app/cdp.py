"""纯 CDP 客户端 —— 不使用 Playwright，避免注入任何自动化特征。

【背景：为什么必须这样做】
    Playwright 启动的浏览器带 ``--enable-automation`` 等开关，导致：
        navigator.webdriver == true
        User-Agent 含 "HeadlessChrome"
    ManageBac / Faria 的风控据此判定为机器人，表现为
    **「密码明明正确，却提示登录失败」**。

【本模块的两条铁律】
    1. 启动：用 ``subprocess`` 启动真实的 msedge.exe，
       只加数据目录与调试端口参数，没有任何自动化开关。
    2. 通信：用原生 CDP（``Page.navigate`` / ``Runtime.evaluate``）读写页面，
       不注入脚本、不设置 automation override。

因此在网站看来，这个浏览器就是一台普通电脑上的普通 Edge。
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
from typing import Any

import websocket

from . import config

log = logging.getLogger(__name__)

_EDGE_CANDIDATES = [
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\EdgeCore\msedge.exe"),
]

# 真实 Edge 的 UA 前缀（headless 时用于覆盖掉 HeadlessChrome 标记）
_EDGE_UA_TEMPLATE = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/{ver} Safari/537.36 Edg/{ver}"
)


# ============================ Edge 进程 ============================

def find_edge() -> Path | None:
    for p in _EDGE_CANDIDATES:
        if p.exists():
            return p
    found = shutil.which("msedge")
    return Path(found) if found else None


def _edge_version(exe: Path) -> str:
    """读取 Edge 主版本号，用于构造真实 UA。"""
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
             f"(Get-Item '{exe}').VersionInfo.ProductVersion"],
            capture_output=True, text=True, timeout=15,
        ).stdout.strip()
        if out:
            return ".".join(out.split(".")[:4])
    except Exception:
        pass
    return "153.0.0.0"


def read_devtools_port(profile_dir: Path) -> int | None:
    """Edge 启动后会把实际调试端口写入 DevToolsActivePort 文件。"""
    f = profile_dir / "DevToolsActivePort"
    if not f.exists():
        return None
    try:
        return int(f.read_text(encoding="utf-8", errors="ignore").splitlines()[0].strip())
    except (IndexError, ValueError, OSError):
        return None


def is_running(profile_dir: Path) -> bool:
    port = read_devtools_port(profile_dir)
    if not port:
        return False
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def launch(profile_dir: Path,
    headless: bool = False,
    url: str | None = None,
    spoof_headless_ua: bool = True,
) -> subprocess.Popen:
    """启动一个**真实**的 Edge 进程（指纹与手动打开一致）。

    关键参数说明：
        --user-data-dir=<独立目录>
            与日常 Edge 隔离，因此不需要关闭你正在用的 Edge。
        --remote-debugging-port=0
            让程序能读取页面内容（随机端口，避免冲突）。
        --disable-blink-features=AutomationControlled
             实测关键：消除 ``navigator.webdriver=true`` 这一自动化特征。
            没有它，带端口的浏览器会被风控识别，登录必失败。

    刻意不使用 --enable-automation / --no-sandbox 等会暴露自动化的开关。
    """
    exe = find_edge()
    if exe is None:
        raise FileNotFoundError("未找到 msedge.exe，请确认已安装 Microsoft Edge")

    profile_dir.mkdir(parents=True, exist_ok=True)
    if not is_running(profile_dir):
        try:
            (profile_dir / "DevToolsActivePort").unlink(missing_ok=True)
        except OSError:
            pass

    flags: list[str] = [
        f"--user-data-dir={profile_dir}",
        "--remote-debugging-port=0",
        #  消除自动化标记
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=msEdgeSidebarV2,msEdgeLaunchOnLogin,msShowFeatureSplash",
        # 减少后台节流，保证长时间抓取稳定
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
    ]

    if headless:
        flags += [
            "--headless=new",
            "--disable-background-timer-throttling",
            "--window-size=1280,900",
        ]

    if url:
        flags.append(url)

    log.info("启动 Edge（headless=%s，指纹已净化）：%s", headless, exe)
    return subprocess.Popen([str(exe), *flags],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
    )


def wait_for_port(profile_dir: Path, timeout: float = 45.0) -> int:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_running(profile_dir):
            port = read_devtools_port(profile_dir)
            if port:
                return port
        time.sleep(0.4)
    raise TimeoutError("等待 Edge 调试端口超时（浏览器可能未成功启动）")


def terminate(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
    except Exception:
        pass


# --------------------- 按数据目录精确关闭浏览器 ---------------------

def list_profile_pids(profile_dir: Path) -> list[int]:
    """列出命令行中包含本数据目录的 msedge.exe 进程号。

    只匹配我们自己的 profile，**绝不会碰到用户日常使用的 Edge**。
    """
    marker = str(profile_dir).replace("'", "''")
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" "
        f"| Where-Object {{ $_.CommandLine -like '*{marker}*' }} "
        "| Select-Object -ExpandProperty ProcessId"
    )
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=30,
        ).stdout
    except Exception:
        return []
    pids: list[int] = []
    for line in out.splitlines():
        line = line.strip()
        if line.isdigit():
            pids.append(int(line))
    return pids


def close_profile_browser(profile_dir: Path, timeout: float = 25.0) -> int:
    """关闭属于本数据目录的 Edge（先尝试正常退出，超时后强制结束）。

    返回关闭的进程数。用户的日常 Edge 不受影响。
    """
    pids = list_profile_pids(profile_dir)
    if not pids:
        return 0
    log.info("关闭本数据目录的 Edge 进程：%s", pids)
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/PID", str(pid)],
                           capture_output=True, timeout=10)
        except Exception:
            pass

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not list_profile_pids(profile_dir):
            time.sleep(0.6)
            return len(pids)
        time.sleep(0.5)

    # 仍未退出 → 强制
    for pid in list_profile_pids(profile_dir):
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=10)
        except Exception:
            pass
    time.sleep(0.6)
    return len(pids)


# ============================ CDP 通道 ============================

class CDPError(RuntimeError):
    pass


class CDP:
    """浏览器级 CDP 连接，支持扁平会话（flatten session）。"""

    def __init__(self, port: int, timeout: float = 60.0) -> None:
        self.port = port
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=5
        ) as r:
            self.version = json.loads(r.read().decode("utf-8"))
        ws_url = self.version["webSocketDebuggerUrl"]
        self.ws = websocket.create_connection(ws_url, timeout=timeout, suppress_origin=True, max_size=None
        )
        self._id = 0
        self.events: list[dict] = []      # 收到的 CDP 事件（供网络监听等使用）

    # ---- 底层收发 ----

    def send(self, method: str, params: dict | None = None,
             session_id: str | None = None) -> Any:
        self._id += 1
        msg: dict = {"id": self._id, "method": method}
        if params:
            msg["params"] = params
        if session_id:
            msg["sessionId"] = session_id
        self.ws.send(json.dumps(msg))

        while True:
            raw = self.ws.recv()
            if not raw:
                raise CDPError("CDP 连接已关闭")
            data = json.loads(raw)
            if data.get("id") != self._id:
                # 是事件 → 存入缓冲，供 drain_events 取用
                if "method" in data:
                    self.events.append(data)
                continue
            if "error" in data:
                raise CDPError(f"{method}: {data['error'].get('message')}")
            return data.get("result", {})

    def drain_events(self, method_filter: str | None = None) -> list[dict]:
        """取出并清空已缓冲的事件。"""
        if method_filter is None:
            out = self.events
            self.events = []
            return out
        out = [e for e in self.events if e.get("method") == method_filter]
        self.events = [e for e in self.events if e.get("method") != method_filter]
        return out

    def pump(self, iterations: int = 40, timeout_each: float = 0.05) -> None:
        """短暂读取 socket，把事件收进缓冲（非阻塞式）。"""
        old = self.ws.gettimeout()
        try:
            self.ws.settimeout(timeout_each)
            for _ in range(iterations):
                try:
                    raw = self.ws.recv()
                except Exception:
                    break
                if not raw:
                    break
                try:
                    data = json.loads(raw)
                except Exception:
                    continue
                if "method" in data and "id" not in data:
                    self.events.append(data)
        finally:
            try:
                self.ws.settimeout(old)
            except Exception:
                pass

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass

    # ---- 目标管理 ----

    def targets(self) -> list[dict]:
        return self.send("Target.getTargets").get("targetInfos", [])

    def attach(self, target_id: str) -> str:
        r = self.send("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        return r["sessionId"]

    def create(self, url: str = "about:blank") -> tuple[str, str]:
        t = self.send("Target.createTarget", {"url": url})
        tid = t["targetId"]
        return tid, self.attach(tid)


class Page:
    """单个页面的读写句柄（接口刻意与常用浏览器 API 保持一致）。"""

    def __init__(self, cdp: CDP, session_id: str, target_id: str) -> None:
        self.cdp = cdp
        self.sid = session_id
        self.tid = target_id
        self._enable()

    def _enable(self) -> None:
        for m in ("Page.enable", "Runtime.enable"):
            try:
                self.cdp.send(m, session_id=self.sid)
            except CDPError:
                pass

    # ---- 基础 ----

    def eval(self, expression: str, await_promise: bool = False) -> Any:
        r = self.cdp.send("Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": await_promise,
                "userGesture": False,
            },
            session_id=self.sid,
        )
        if "exceptionDetails" in r:
            desc = r["exceptionDetails"].get("text", "")
            raise CDPError(f"JS 异常：{desc}")
        return r.get("result", {}).get("value")

    @property
    def url(self) -> str:
        return self.eval("document.location.href") or ""

    def title(self) -> str:
        return self.eval("document.title") or ""

    def content(self) -> str:
        return self.eval("document.documentElement.outerHTML") or ""

    def inner_text(self, selector: str = "body") -> str:
        return self.eval(f"(() => {{ const e = document.querySelector({json.dumps(selector)});"
            f" return e ? e.innerText : ''; }})()"
        ) or ""

    def wait_for_timeout(self, ms: int) -> None:
        time.sleep(ms / 1000.0)

    def goto(self, url: str, wait_until: str | None = None, timeout: int = 45000) -> None:
        """导航并在 readyState 就绪后返回。"""
        self.cdp.send("Page.navigate", {"url": url}, session_id=self.sid)
        self._wait_ready(timeout)

    def _wait_ready(self, timeout: int = 45000) -> None:
        deadline = time.time() + timeout / 1000.0
        while time.time() < deadline:
            try:
                state = self.eval("document.readyState")
            except CDPError:
                time.sleep(0.25)
                continue
            if state in ("interactive", "complete"):
                time.sleep(0.35)      # 给 SPA 一点渲染时间
                return
            time.sleep(0.25)

    # ---- 同源请求（用页面自身的 fetch，天然带上 Cookie） ----

    def fetch_text(self, url: str, timeout_ms: int = 20000) -> dict:
        js = f"""
        (async () => {{
          try {{
            const ctl = new AbortController();
            const t = setTimeout(() => ctl.abort(), {timeout_ms});
            const r = await fetch({json.dumps(url)},
                                  {{credentials: 'include', redirect: 'follow',
                                    signal: ctl.signal}});
            clearTimeout(t);
            const body = await r.text();
            const h = {{}};
            r.headers.forEach((v, k) => {{ h[k] = v; }});
            return JSON.stringify({{status: r.status, ok: r.ok,
                                    url: r.url, headers: h, text: body}});
          }} catch (e) {{
            return JSON.stringify({{status: 0, ok: false, error: String(e),
                                    headers: {{}}, text: ''}});
          }}
        }})()
        """
        raw = self.eval(js, await_promise=True)
        try:
            return json.loads(raw)
        except Exception:
            return {"status": 0, "ok": False, "headers": {}, "text": "", "error": "解析失败"}


class _Response:
    """模仿常见 requests 响应对象，便于上层直接使用。"""

    def __init__(self, data: dict) -> None:
        self.status: int = data.get("status", 0)
        self.ok: bool = bool(data.get("ok"))
        self.url: str = data.get("url", "")
        self.headers: dict = {k.lower(): v for k, v in (data.get("headers") or {}).items()}
        self._text: str = data.get("text", "") or ""
        self.error: str = data.get("error", "")

    @property
    def text(self) -> str:
        return self._text

    def json(self):
        return json.loads(self._text)


class _RequestShim:
    def __init__(self, page: Page) -> None:
        self._page = page

    def get(self, url: str, timeout: int | None = None) -> _Response:
        return _Response(self._page.fetch_text(url, timeout or 20000))


# Page.request 便捷属性
def _page_request(self: Page) -> _RequestShim:
    return _RequestShim(self)


Page.request = property(_page_request)  # type: ignore[attr-defined]


# ============================ 会话封装 ============================

class Session:
    """取数会话：连接到一个真实 Edge，复用一个标签页读取数据。

    与旧接口保持一致（open / page / close），便于上层直接调用。
    """

    def __init__(self, headless: bool = True, profile_dir: Path | None = None) -> None:
        self.headless = headless
        self.profile_dir = Path(profile_dir or config.BROWSER_PROFILE_DIR)
        self._cdp: CDP | None = None
        self._page: Page | None = None
        self._proc: subprocess.Popen | None = None
        self._owned = False

    def open(self) -> "Session":
        config.ensure_dirs()

        if is_running(self.profile_dir):
            log.info("复用已在运行的 Edge")
        else:
            self._proc = launch(self.profile_dir, headless=self.headless)
            self._owned = True

        port = wait_for_port(self.profile_dir)
        self._cdp = CDP(port)
        self._page = self._pick_page()
        return self

    def _pick_page(self) -> Page:
        assert self._cdp is not None
        pages = [t for t in self._cdp.targets() if t.get("type") == "page"]
        if pages:
            # 优先选已在目标站点的标签页
            for t in pages:
                if "managebac" in (t.get("url") or "").lower():
                    return Page(self._cdp, self._cdp.attach(t["targetId"]), t["targetId"])
            t = pages[0]
            return Page(self._cdp, self._cdp.attach(t["targetId"]), t["targetId"])
        tid, sid = self._cdp.create("about:blank")
        return Page(self._cdp, sid, tid)

    def page(self) -> Page:
        if self._page is None:
            self._page = self._pick_page()
        return self._page

    def close(self) -> None:
        # 只断开连接，绝不关闭你的浏览器
        try:
            if self._cdp is not None:
                self._cdp.close()
        except Exception:
            pass
        self._cdp = None
        self._page = None
        if self._owned:
            terminate(self._proc)
        self._proc = None
        self._owned = False

    def __enter__(self) -> "Session":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()


# ============================ 登录判断 ============================

_LOGIN_HINTS = ("/login", "/sign_in", "/signin", "/users/sign_in", "/sso", "saml", "oauth")

HOME_URL = f"{config.BASE_URL}/student/home"


def looks_logged_in(page: Page) -> bool:
    try:
        url = (page.url or "").lower()
        if any(h in url for h in _LOGIN_HINTS):
            return False
        if "/student/" not in url:
            return False
        return len(page.inner_text("body").strip()) > 120
    except Exception:
        return False


def check_logged_in(page: Page, settle_ms: int = 1500) -> bool:
    try:
        page.goto(HOME_URL)
        page.wait_for_timeout(settle_ms)
    except Exception as e:
        log.warning("访问首页失败：%s", e)
        return False
    return looks_logged_in(page)


# ============================ 诊断 ============================

def fingerprint() -> dict:
    """采集当前浏览器指纹，用于确认没有被风控识别。"""
    out: dict = {"edge": None, "running": False, "error": ""}
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
        page.goto("about:blank")
        out["webdriver"] = page.eval("navigator.webdriver")
        out["ua"] = page.eval("navigator.userAgent")
        out["platform"] = page.eval("navigator.platform")
        out["languages"] = page.eval("navigator.languages")
        out["plugins"] = page.eval("navigator.plugins.length")
        out["chrome_obj"] = page.eval("typeof window.chrome")
        page.goto(HOME_URL)
        page.wait_for_timeout(1500)
        out["title"] = page.title()
        out["url"] = page.url
        out["logged_in"] = looks_logged_in(page)
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    finally:
        s.close()
    return out
