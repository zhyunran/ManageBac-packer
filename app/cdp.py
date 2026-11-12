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
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import websocket

from . import config, proc

log = logging.getLogger(__name__)


# ============================ Edge 进程 ============================

def find_edge() -> Path | None:
    """定位可用的 Chromium 浏览器。

    ★ 名字保留是为了不惊动一堆调用点，但**不再只认 Edge**：
      默认浏览器（Chrome/Edge/Brave/Chromium 任一）都可以。
      见 app/browser.py 与 config.BROWSER。
    """
    from . import browser as _browser
    return _browser.find()


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


# ── 日常浏览器的 User Data 目录（复用登录态时用）──
_USER_DATA_DIRS = {
    "msedge.exe": ("Microsoft", "Edge", "User Data"),
    "chrome.exe": ("Google", "Chrome", "User Data"),
    "brave.exe": ("BraveSoftware", "Brave-Browser", "User Data"),
}


def _user_data_dir_of(exe: Path) -> Path | None:
    """这个浏览器装在哪，它的日常 User Data 目录就在哪。"""
    name = exe.name.lower()
    parts = _USER_DATA_DIRS.get(name)
    if not parts and "chromium" in str(exe).lower():
        parts = ("Chromium", "User Data")
    if not parts:
        return None
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        return None
    d = Path(base).joinpath(*parts)
    return d if d.is_dir() else None


def _profile_in_use(profile_dir: Path) -> bool:
    """这个 user-data-dir 是否已被某个在跑的浏览器占用。"""
    try:
        return bool(list_profile_pids(profile_dir))
    except Exception:
        return False


def _effective_profile(profile_dir: Path, exe: Path) -> Path:
    """决定本次用哪个 user-data-dir。

    默认用自己的独立 profile；
    开了 reuse 且日常浏览器没在跑 → 用日常的（直接继承登录态）。
    """
    try:
        reuse = bool(getattr(config, "BROWSER_REUSE_PROFILE", False))
    except Exception:
        reuse = False
    if not reuse:
        return profile_dir

    real = _user_data_dir_of(exe)
    if real is None:
        log.info("复用登录态：找不到日常浏览器的 User Data 目录，改用独立 profile")
        return profile_dir
    if _profile_in_use(real):
        #  ★ 这时候硬用会失败：浏览器会把新进程转交给已开实例，CDP 端口起不来。
        log.info("复用登录态：%s 正在运行，先退回独立 profile"
                 "（关掉它之后下次自动继承登录态）", real)
        return profile_dir
    log.info("复用登录态：使用日常浏览器的数据目录 %s", real)
    return real


def launch(profile_dir: Path,
    headless: bool = False,
    url: str | None = None,
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

    #  ★ 先决定用哪个 user-data-dir ★
    #    开了「复用日常浏览器登录」且那个浏览器没在跑时，直接用它的真实目录，
    #    这样 Chrome 里登录过的 Teams，程序拿来就能用，不用再登一次。
    profile_dir = _effective_profile(profile_dir, exe)

    profile_dir.mkdir(parents=True, exist_ok=True)
    if not is_running(profile_dir):
        #  ★ 同 edge.py 的坑：旧实例正在退出时立刻起新的，
        #    单实例锁会让新进程「转手即退」，端口永远不来。
        #    先确认同 profile 的旧进程退干净再启动。
        try:
            if list_profile_pids(profile_dir):
                log.info("旧 Edge 实例还没退完，先收掉再起新的")
                close_profile_browser(profile_dir)
        except Exception as e:
            log.debug("清旧实例失败（不挡启动）：%s", e)
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
        #  关掉各种边栏 / 首次运行引导 / 「打开桌面应用」类提示
        "--disable-features=msEdgeSidebarV2,msEdgeLaunchOnLogin,"
        "msShowFeatureSplash,msEdgeAppLauncher,"
        "EdgeOpenExternalProtocolPrompt",
        #  减少后台节流，保证长时间抓取稳定
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
    ]

    #  ★★ 不走系统代理 ★★
    #    和 httpclient.no_proxy_handler() 同一个理由，但这里容易漏：
    #    Python 的网络请求绕开了代理，浏览器却仍然读系统设置。
    #    实测后果不是"慢"，而是**页面直接残废** ——
    #    希悦登录页是 React SPA，静态资源加载失败后只剩未编译模板：
    #        buttons: "<% $t('登录') %>"
    #        inputs : 只有 school_id，没有用户名/密码框
    #    于是自动填表永远 "no input"，课表永远空白。
    #    需要走代理时设 MBPK_USE_SYSTEM_PROXY=1。
    _use_proxy = (os.environ.get("MBPK_USE_SYSTEM_PROXY") or "").strip().lower()
    if _use_proxy not in ("1", "true", "yes", "on"):
        flags.append("--no-proxy-server")

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


def terminate(popen_obj: subprocess.Popen | None) -> None:
    """正常结束一个我们启动的进程。

    ★ 参数名不要叫 `proc` —— 会遮蔽本模块导入的 `app.proc`。
    """
    if popen_obj is None:
        return
    try:
        if popen_obj.poll() is None:
            popen_obj.terminate()
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
        #  ★ 必须走 proc.run —— 直接 subprocess 会弹控制台黑框，
        #    而这里在循环里被反复调用（0.5.1 的「狂跳黑框」就是这么来的）
        out = proc.run(["powershell", "-NoProfile", "-Command", ps],
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
    #  ★ 用 ctypes 直接杀，不用 taskkill —— 后者是外部程序，
    #    每个 pid 都要 spawn 一个（会闪黑框，且在某些场景会卡）。
    for pid in pids:
        try:
            proc.kill_pid(pid, force=False)
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
            proc.kill_pid(pid, force=True)
        except Exception:
            pass
    time.sleep(0.6)
    return len(pids)


# ============================ CDP 通道 ============================

class CDPError(RuntimeError):
    pass


#  通用点击：派发一整套真实鼠标事件。
#  React / Teams 这类应用只认 pointer 序列，单发一个 click 会被忽略。
_JS_CLICK = r"""
(sel, idx) => {
  const els = document.querySelectorAll(sel);
  if (els.length <= idx) return false;
  const e = els[idx];
  try { e.scrollIntoView({block: 'center', inline: 'center'}); } catch (x) {}
  const r = e.getBoundingClientRect();
  const cx = r.x + r.width / 2, cy = r.y + r.height / 2;
  const mk = t => new MouseEvent(t, {
    bubbles: true, cancelable: true, view: window,
    clientX: cx, clientY: cy, button: 0, buttons: 1,
  });
  try { e.focus && e.focus(); } catch (x) {}
  for (const t of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
    try { e.dispatchEvent(mk(t)); } catch (x) {}
  }
  return true;
}
"""

_JS_SCROLL_INTO = r"""
(sel, idx) => {
  const els = document.querySelectorAll(sel);
  if (els.length <= idx) return false;
  try { els[idx].scrollIntoView({block: 'center'}); } catch (x) { return false; }
  return true;
}
"""


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
             session_id: str | None = None,
             timeout: float | None = None) -> Any:
        """发一条 CDP 命令，等它的响应。

        ★ 参数 ``timeout``：临时改这次等待的 socket 超时。
          为什么要这个 ——

          ``recv()`` 会把**事件**（没有 id 的那些）存进缓冲，
          然后 `continue` 继续读，直到读到自己的响应为止。
          正常情况下没问题，但有些命令会**引发大量事件**
          （实测：`Input.dispatchMouseEvent` 发一次 mouseWheel，
          Teams 的虚拟列表会跟着吐出一堆 DOM/滚动事件）。
          事件越多，`recv()` 被调用的次数越多 ——
          如果 socket 超时是默认那 60 秒，而且事件偏偏很密，
          就有可能在「还没读到响应」时被 WebSocket 判超时：

              WebSocketTimeoutException: Connection timed out

          给一个短一点的超时（比如 8 秒），
          失败得快，调用方也好重试。
        """
        self._id += 1
        msg: dict = {"id": self._id, "method": method}
        if params:
            msg["params"] = params
        if session_id:
            msg["sessionId"] = session_id

        old_to = None
        if timeout is not None:
            try:
                old_to = self.ws.gettimeout()
                self.ws.settimeout(timeout)
            except Exception:
                old_to = None

        try:
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
        finally:
            if old_to is not None:
                try:
                    self.ws.settimeout(old_to)
                except Exception:
                    pass

    def fire(self, method: str, params: dict | None = None,
             session_id: str | None = None) -> int:
        """发一条 CDP 命令，**不等响应**，立刻返回。

        ★ 什么时候需要这个 ——
          有些命令会引发**非常多的事件**，而 ``send()`` 的循环是
          「recv → 不是我的响应就继续 recv」，事件一多就容易在
          读到响应前被 socket 超时打断：

              WebSocketTimeoutException: Connection timed out

          实测 `Input.dispatchMouseEvent` 发一次 mouseWheel
          就会碰上（Teams 的虚拟列表会跟着吐一堆事件）。

          这类命令**本来就不需要响应** —— 要的是「把输入塞进去」，
          效果看后面的 DOM 变化就行。所以用 fire 发出去就走，
          socket 上的事件留给下一次 send / pump 顺手清掉。

        返回这次用的消息 id（便于在 events 里对号）。
        """
        self._id += 1
        msg: dict = {"id": self._id, "method": method}
        if params:
            msg["params"] = params
        if session_id:
            msg["sessionId"] = session_id
        self.ws.send(json.dumps(msg))
        return self._id

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

    def _exc(self, r: dict, what: str) -> None:
        if "exceptionDetails" not in r:
            return
        d = r["exceptionDetails"]
        ex = d.get("exception") or {}
        extra = ""
        if isinstance(ex, dict):
            extra = str(ex.get("description") or ex.get("value") or "")
        raise CDPError(f"JS 异常({what})：{d.get('text','')} {extra[:300]}".strip())

    #  统一包装：求值 → 是函数就调 → 是 Promise 就等。
    #  为什么要这么绕：Runtime.evaluate 只认「表达式」，且 returnByValue 打开后
    #  函数拿不到 objectId，没法再补调。包一层 async IIFE 就一次到位。
    _WRAP = (
        "(async () => {"
        "  let __r = (EXPR);"
        "  if (typeof __r === 'function') { __r = __r(); }"
        "  if (__r && typeof __r.then === 'function') { __r = await __r; }"
        "  return __r;"
        "})()"
    )

    def eval(self, expression: str, await_promise: bool = True) -> Any:
        """求值一个 JS 表达式并取回值。

        自动兼容三种写法（都是踩过的坑）：
            ``() => 2``        函数 —— 替它调一次
            ``async () => 2``  Promise —— 等它落地
            ``1 + 1``          普通表达式 —— 直接取值
        """
        #  末尾分号会让 `let __r = (EXPR)` 变成语法错误，先剥掉
        expr = expression.strip().rstrip(";").strip()
        src = self._WRAP.replace("EXPR", f"\n{expr}\n")
        r = self._raw_eval(src, await_promise=True)
        self._exc(r, "eval")
        return (r.get("result") or {}).get("value")

    def call(self, fn_source: str, *args: Any) -> Any:
        """调用带参数的 JS 函数：``p.call("(a,b)=>a+b", 1, 2)``。

        参数走 JSON，不用操心引号转义。
        """
        payload = json.dumps(list(args), ensure_ascii=False)
        return self.eval(f"({fn_source}).apply(null, {payload})")

    def _raw_eval(self, expression: str, await_promise: bool = False) -> dict:
        return self.cdp.send("Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": await_promise,
                "userGesture": False,
            },
            session_id=self.sid,
        )

    def click(self, selector: str, index: int = 0) -> bool:
        """用真实鼠标事件点第 ``index`` 个匹配元素。"""
        return bool(self.call(_JS_CLICK, selector, index))

    def scroll_to(self, selector: str, index: int = 0) -> bool:
        return bool(self.call(_JS_SCROLL_INTO, selector, index))

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
