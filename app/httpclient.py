"""纯 HTTP 会话 —— 不依赖浏览器。

【重大简化】实测发现 ManageBac 用普通 HTTP 请求就能完整抓取：
    POST /sessions                        → 302 登录成功
    GET  /student/home                    → 200
    GET  /student/classes/{cid}/core_tasks → 200

因此本模块提供与 `cdp.Session` / `cdp.Page` **接口兼容**的实现，
`scraper.Scraper` 无需任何修改即可直接使用。

优势：
    * 快      —— 几秒内（浏览器要几十秒启动）
    * 稳      —— 不依赖 Edge 启动 / 页面渲染节奏
    * 轻      —— 开机预热开销很小
    * 无风控  —— 普通 HTTP 请求，没有 webdriver 之类特征

会话持久化：`_managebac_session` 是**持久 Cookie**（30 天），
存到 `data/session_cookies.json`，登录一次可用很久。
"""
from __future__ import annotations

import http.cookiejar
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import config

log = logging.getLogger("httpclient")

BASE = config.BASE_URL
SESSION_FILE = config.DATA_DIR / "session_cookies.json"
CRED_FILE = config.ROOT / "credentials.json"
COOLDOWN_FILE = config.DATA_DIR / "rate_limit.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36 Edg/153.0.0.0"
)

HOME = f"{BASE}/student/home"

# 请求超时（秒）。原来 45 秒太长 —— 被限速时每个请求都拖满，
# 一轮抓取能拖到两三分钟，看起来像卡死。15 秒足够（正常响应 < 2 秒）。
REQ_TIMEOUT = 15

# 命中 429 后的冷却时长（秒）。
# 站点限速是按时间窗的，短时间内继续请求只会一直 429。
# 冷却期内所有进程都不再发请求，直接读缓存。
COOLDOWN_SEC = 240


# ---------------------------------------------------------------- 限速冷却

_cooldown_lock = threading.Lock()


def set_cooldown(seconds: float = COOLDOWN_SEC, reason: str = "429") -> None:
    """记录一次限速，让**所有进程**在冷却期内不再发请求。"""
    global _cooldown_mem
    with _cooldown_lock:
        until = time.time() + seconds
        _cooldown_mem = until
        try:
            config.ensure_dirs()
            COOLDOWN_FILE.write_text(json.dumps({"until": until, "reason": reason,
                            "at": time.strftime("%Y-%m-%d %H:%M:%S")}),
                encoding="utf-8",
            )
        except Exception as e:
            log.warning("写入冷却标记失败：%s", e)
        log.warning("已进入 %.0f 秒冷却（%s）", seconds, reason)


_cooldown_mem: float = 0.0


def cooldown_remaining() -> float:
    """还剩多少秒冷却。0 表示可以正常请求。"""
    best = 0.0
    now = time.time()
    if _cooldown_mem > now:
        best = _cooldown_mem - now
    try:
        d = json.loads(COOLDOWN_FILE.read_text(encoding="utf-8"))
        until = float(d.get("until") or 0)
        if until > now:
            best = max(best, until - now)
    except Exception:
        pass
    return best


def clear_cooldown() -> None:
    global _cooldown_mem
    with _cooldown_lock:
        _cooldown_mem = 0.0
        try:
            COOLDOWN_FILE.unlink(missing_ok=True)
        except Exception:
            pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """不自动跟随重定向 —— 登录成功的 302 本身就是重要信号。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


# ---------------------------------------------------------------- 凭据

def load_credentials() -> dict:
    try:
        d = json.loads(CRED_FILE.read_text(encoding="utf-8"))
        return d.get("_managebac", {}) or {}
    except Exception as e:
        log.warning("读取凭据失败：%s", e)
        return {}


# ---------------------------------------------------------------- Cookie 存取

def _cookies_to_json(cj: http.cookiejar.CookieJar) -> list[dict]:
    return [
        {
            "name": c.name, "value": c.value, "domain": c.domain,
            "path": c.path or "/", "expires": c.expires, "secure": c.secure,
        }
        for c in cj
    ]


def _json_to_cookies(cj: http.cookiejar.CookieJar, raw: list[dict]) -> int:
    n = 0
    for d in raw:
        try:
            cj.set_cookie(http.cookiejar.Cookie(version=0, name=d["name"], value=d["value"], port=None,
                port_specified=False, domain=d["domain"], domain_specified=True,
                domain_initial_dot=str(d["domain"]).startswith("."),
                path=d.get("path", "/"), path_specified=True,
                secure=bool(d.get("secure")), expires=d.get("expires"),
                discard=False, comment=None, comment_url=None, rest={},
            ))
            n += 1
        except Exception:
            pass
    return n


# 保存会话 Cookie 的全局锁 —— 并行工作线程会同时写，必须串行化
_save_lock = threading.Lock()
_save_done_at: float = 0.0


def save_cookies(cj: http.cookiejar.CookieJar, force: bool = False) -> None:
    """保存会话 Cookie（线程安全）。

    只在**拿到了登录 Cookie** 时才写，避免：
      * 把空 jar 写进去，反而把好会话冲掉
      * 并行线程互相覆盖

    并且：非强制时，10 分钟内只写一次（会话不会这么快变）。
    """
    global _save_done_at

    try:
        data = _cookies_to_json(cj)
        names = {d.get("name") for d in data if d.get("value")}
        if "_managebac_session" not in names:
            # 这个 jar 里没有登录会话 —— 不动磁盘上的文件
            return

        with _save_lock:
            if not force and (time.time() - _save_done_at) < 600:
                return

            config.ensure_dirs()
            # 用「进程 + 线程」唯一的临时名，避免并发写冲突
            tmp = SESSION_FILE.with_suffix(f".json.tmp.{os.getpid()}.{threading.get_ident()}"
            )
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            # Windows 上 replace 可能被短暂占用 → 重试几次
            for i in range(5):
                try:
                    tmp.replace(SESSION_FILE)
                    break
                except PermissionError:
                    if i == 4:
                        raise
                    time.sleep(0.25 * (i + 1))
            _save_done_at = time.time()
    except Exception as e:
        log.warning("保存会话 Cookie 失败：%s", e)


def load_cookies() -> list[dict]:
    try:
        return json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def has_saved_session() -> bool:
    """有未过期的会话 Cookie 吗？"""
    for d in load_cookies():
        if d.get("name") == "_managebac_session" and d.get("value"):
            exp = d.get("expires")
            if not exp or exp > time.time():
                return True
    return False


def cookies_for_download() -> str:
    """拼成 HTTP Cookie 头，供 download.py 复用。"""
    return "; ".join(f"{d['name']}={d['value']}" for d in load_cookies() if d.get("value")
    )


def browser_cookies_dict() -> dict[str, str]:
    """返回 {名称: 值} 形式的 Cookie（下载时用）。

    优先用已保存的 HTTP 会话；拿不到时返回空字典
    （不会去启动浏览器 —— 下载对匿名/失效会明确报错，更好排查）。
    """
    out: dict[str, str] = {}
    for d in load_cookies():
        if d.get("value"):
            out[str(d["name"])] = str(d["value"])
    return out


def clear_session() -> None:
    try:
        SESSION_FILE.unlink(missing_ok=True)
    except Exception:
        pass


# ---------------------------------------------------------------- 登录

LOGIN_ERR_JS = re.compile(r"alert-content'>([^<]+)", re.I
)


def parse_login_error(html: str) -> str:
    """从登录失败页面抠出服务端提示。"""
    m = LOGIN_ERR_JS.search(html)
    if m:
        return " ".join(m.group(1).split())[:300]
    for kw in ("temporarily locked", "Invalid email or password"):
        if kw.lower() in html.lower():
            return kw
    return ""


def login(login_id: str, password: str, timeout: int = REQ_TIMEOUT) -> tuple[bool, str]:
    """用凭据登录取会话。返回 (成功?, 说明)。

     只在必要时调用；失败时**绝不自动重试**
      （连续 5 次失败会锁定账号）。
    """
    # 冷却期内绝不尝试登录 —— 登录也是请求，只会加重限速
    cd = cooldown_remaining()
    if cd > 0:
        return False, f"站点限速冷却中（还剩 {int(cd)} 秒），稍后再试"

    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj), NoRedirect()
    )
    op.addheaders = [  # type: ignore[attr-defined]
        ("User-Agent", UA),
        ("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
        ("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8"),
        ("Accept-Encoding", "identity"),
    ]

    # 1) 拿 CSRF token
    try:
        resp = op.open(f"{BASE}/login", timeout=timeout)
        html = resp.read().decode("utf-8", "replace")
    except Exception as e:
        return False, f"打开登录页失败：{type(e).__name__}: {e}"

    m = re.search(r'name="authenticity_token" value="([^"]+)"', html)
    if not m:
        return False, "登录页里找不到 authenticity_token（站点结构可能变了）"
    token = m.group(1)

    # 2) 提交
    data = urllib.parse.urlencode({
        "authenticity_token": token,
        "login": login_id,
        "password": password,
        "remember_me": "1",
        "commit": "Sign in",
    }).encode()

    req = urllib.request.Request(f"{BASE}/sessions", data=data)
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Referer", f"{BASE}/login")
    req.add_header("Origin", BASE)

    try:
        resp2 = op.open(req, timeout=timeout)
        st = resp2.status
        hd = dict(resp2.headers)
        body = resp2.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        st = e.code
        hd = dict(e.headers)
        body = e.read().decode("utf-8", "replace")
        if st == 429:
            set_cooldown(COOLDOWN_SEC, "登录时被限速")
            return False, "站点限速中（429），已进入冷却"
    except Exception as e:
        return False, f"提交登录失败：{type(e).__name__}: {e}"

    # 3) 判断
    if st in (301, 302, 303, 307, 308):
        loc = hd.get("Location") or hd.get("location") or ""
        save_cookies(cj, force=True)      #  新会话必须立刻落盘
        clear_cooldown()                  # 登录成功 → 限速大概已解除
        return True, f"登录成功 → {loc}"

    detail = parse_login_error(body)
    low = detail.lower()
    if "locked" in low:
        return False, f"账号被临时锁定：{detail}"
    if "invalid" in low or "incorrect" in low:
        return False, f"账号或密码不正确：{detail}"
    return False, f"登录被拒（HTTP {st}）：{detail or '服务端未给出原因'}"


def auto_login() -> tuple[bool, str]:
    """用 credentials.json 里的凭据登录一次（**不重试**）。

     安全：连续 5 次失败会锁定账号，因此这里绝不自动重试。
    """
    cred = load_credentials()
    lid = (cred.get("login") or "").strip()
    pw = (cred.get("password") or "").strip()
    if not lid or not pw:
        return False, "还没有填过账号，点下面的按钮填一次就行"

    # 登录前先确认现有会话真的失效了（避免多余的登录尝试）
    if has_saved_session() and cooldown_remaining() <= 0:
        cj = http.cookiejar.CookieJar()
        _json_to_cookies(cj, load_cookies())
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj), NoRedirect()
        )
        op.addheaders = [("User-Agent", UA)]  # type: ignore[attr-defined]
        try:
            with op.open(HOME, timeout=REQ_TIMEOUT) as r:
                body = r.read(4000).decode("utf-8", "replace")
            if r.status == 200 and "session_form" not in body:
                log.info("现有会话仍然有效，跳过登录")
                return True, "现有会话有效"
        except urllib.error.HTTPError as e:
            if e.code == 429:
                set_cooldown(COOLDOWN_SEC)
                return False, "站点限速中（429），暂不登录"
        except Exception:
            pass

    return login(lid, pw)


# ---------------------------------------------------------------- Page 兼容层

class HttpPage:
    """模拟 `cdp.Page` 的最小接口，让 Scraper 无需改动。

    Scraper 只用到三个方法：
        goto(url) / wait_for_timeout(ms) / content()
    """

    def __init__(self, session: "HttpSession") -> None:
        self._s = session
        self._html = ""
        self._url = "about:blank"

    # ---- Scraper 用到的三个 ----

    def goto(self, url: str, timeout: int = 45000) -> None:
        self._url = url
        self._html = self._s.get_text(url)

    def wait_for_timeout(self, ms: int) -> None:
        # HTTP 是同步的：页面一拿到就是完整的 HTML，
        # 不需要等渲染 / 等 JS。这里**直接返回**，不睡眠。
        # 这正是 HTTP 模式比浏览器快得多的原因。
        return

    def content(self) -> str:
        return self._html

    # ---- 额外便利方法 ----

    @property
    def url(self) -> str:
        return self._url

    def inner_text(self, selector: str = "body") -> str:
        """粗略提取文本（不引解析器，够用即可）。"""
        try:
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(self._html, "html.parser")
            el = soup.select_one(selector)
            return el.get_text(" ", strip=True) if el else ""
        except Exception:
            return ""

    def eval(self, _js: str, await_promise: bool = False):
        """HTTP 模式不支持在页面里执行 JS。

        schedule.py 会用它读 localStorage —— 那条路在 HTTP 模式下走不通，
        需要改用其它持久化方式（见 `app/schedule.py`）。
        """
        raise NotImplementedError("HTTP 模式不支持 eval（无 JS 环境）")


# ---------------------------------------------------------------- Session 兼容层

class HttpSession:
    """模拟 `cdp.Session`，供 Scraper / pipeline 直接替换使用。"""

    def __init__(self, profile_dir: Path | None = None, headless: bool = True,
                 auto_login_on_demand: bool = True) -> None:
        self.headless = headless          # 仅为兼容签名，HTTP 模式无意义
        self.profile_dir = profile_dir
        self.auto_login_on_demand = auto_login_on_demand
        self._cj = http.cookiejar.CookieJar()
        self._op = self._make_opener()
        self._page: HttpPage | None = None
        self.logged_in = False
        self.last_error = ""
        self.rate_limited = False      # 被站点限速（429）

    # ---- 内部 ----

    def _make_opener(self) -> urllib.request.OpenerDirector:
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self._cj)
        )
        op.addheaders = [  # type: ignore[attr-defined]
            ("User-Agent", UA),
            ("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
            ("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8"),
            ("Accept-Encoding", "identity"),
        ]
        return op

    def _request(self, url: str, referer: str | None = None,
                 timeout: int = REQ_TIMEOUT) -> tuple[int, str, str]:
        """返回 (status, final_url, text)。"""
        # 冷却期内直接拒绝，不发任何请求
        cd = cooldown_remaining()
        if cd > 0:
            self.rate_limited = True
            log.debug("冷却中（还剩 %.0f 秒），跳过请求", cd)
            return 429, url, ""

        req = urllib.request.Request(url)
        if referer:
            req.add_header("Referer", referer)
        try:
            with self._op.open(req, timeout=timeout) as resp:
                raw = resp.read()
                enc = "utf-8"
                ct = resp.headers.get("Content-Type", "")
                m = re.search(r"charset=([\w-]+)", ct)
                if m:
                    enc = m.group(1)
                return resp.status, resp.geturl(), raw.decode(enc, "replace")
        except urllib.error.HTTPError as e:
            if e.code == 429:
                #  立刻进入全局冷却，避免继续冲击站点
                self.rate_limited = True
                set_cooldown(COOLDOWN_SEC)
            return e.code, url, e.read().decode("utf-8", "replace")
        except Exception as e:
            log.warning("请求失败 %s：%s", url, e)
            return 0, url, ""

    # ---- 生命周期 ----

    def reload_cookies(self) -> int:
        """把磁盘上的会话 Cookie 重新装进本会话。

         关键：`auto_login()` 是在**另一个** opener 里登录的，
          它的 Cookie 由 `save_cookies()` 写到磁盘。
          本会话必须重新装载，否则它仍然带着空的 / 过期的 jar，
          表现为「明明登录成功了，却还说自己没登录」。
        """
        raw = load_cookies()
        if not raw:
            return 0
        # 清掉旧的同名 Cookie，避免出现两份
        self._cj.clear()
        return _json_to_cookies(self._cj, raw)

    def ensure_login(self) -> tuple[bool, str]:
        """确保本会话处于登录态。必要时重新登录并**重新装载 Cookie**。"""
        if self.check_login():
            self.logged_in = True
            return True, "已是登录状态"

        #  被限速时不要尝试登录 —— 等一会儿就好，硬试只会更糟
        if self.rate_limited:
            self.logged_in = False
            self.last_error = "站点限速中（429），请稍后重试"
            return False, self.last_error

        ok, msg = auto_login()
        if not ok:
            self.logged_in = False
            self.last_error = msg
            return False, msg

        #  把刚登录得到的 Cookie 装进本会话
        n = self.reload_cookies()
        if self.check_login():
            self.logged_in = True
            self.last_error = ""
            return True, f"登录成功（已装载 {n} 个 Cookie）"

        self.logged_in = False
        self.last_error = ("站点限速中（429）" if self.rate_limited
                           else "登录成功但会话仍无效")
        return False, self.last_error

    def open(self, verify: bool = True) -> "HttpSession":
        """装载已保存的会话；无效就尝试自动登录（**只一次**）。

        Args:
            verify: 是否发一个请求验证登录态。
                 **默认 True**（保持原有行为），但调用方如果**马上就要
                  请求一个需要登录的页面**，应该传 `verify=False` ——
                  因为那一次请求本身就能告诉我们登录是否有效，
                  额外再 GET 一个 476 KB 的首页纯属浪费
                  （实测占详情页耗时的 70%，1294 ms）。
        """
        raw = load_cookies()
        if raw:
            n = _json_to_cookies(self._cj, raw)
            log.info("已装载 %d 个会话 Cookie", n)

        if verify:
            self.logged_in = self.check_login()

            if not self.logged_in and self.auto_login_on_demand:
                log.info("会话无效，尝试自动登录……")
                self.ensure_login()
                if not self.logged_in:
                    log.warning("自动登录未成功：%s", self.last_error)
        else:
            # 不做网络验证 —— 只用「本地有没有 Cookie」做粗略判断。
            # 真正的结论由后续那个页面请求给出。
            self.logged_in = bool(raw)

        return self

    def page(self) -> HttpPage:
        if self._page is None:
            self._page = HttpPage(self)
        return self._page

    def close(self) -> None:
        save_cookies(self._cj)
        self._page = None

    def __enter__(self) -> "HttpSession":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- 供 Page 调用 ----

    def get_text(self, url: str, retries: int = 2) -> str:
        """取页面 HTML。失败重试（网络抖动，不是登录问题）。"""
        st, _final, text = self._request(url)
        if st == 200 and text and "session_form" not in text[:4000]:
            return text

        # 会话掉了 → 重登一次再说（ensure_login 会重新装载 Cookie）
        if st in (301, 302, 303, 307, 308) or "/login" in (_final or ""):
            ok, msg = self.ensure_login()
            if ok:
                st2, _, text2 = self._request(url)
                if st2 == 200:
                    return text2
            self.last_error = msg
            return text

        for i in range(retries):
            time.sleep(0.8 * (i + 1))
            st2, _, text2 = self._request(url)
            if st2 == 200 and text2:
                return text2
        return text

    def check_login(self) -> bool:
        """只读探测：GET /student/home 看是否被踢到登录页。

         429 = 被限速，**不代表会话失效** —— 此时绝不能重新登录
          （多余的登录尝试可能累积失败次数 → 锁定账号）。
        """
        st, final, text = self._request(HOME)

        if st == 429:
            # 被限速：保守起见当作「会话仍有效」，让调用方稍后重试
            self.rate_limited = True
            log.warning("触发站点限速（429），本次不重试登录")
            return True

        self.rate_limited = False
        if st != 200:
            return False
        if "/login" in (final or "") or "session_form" in text[:6000]:
            return False
        # 出现学生页特征
        return "student" in text.lower() or "fusion" in text.lower()

    # ---- 便利 ----

    def download(self, url: str, timeout: int = 120,
                 referer: str | None = None) -> tuple[int, bytes, str]:
        """下载二进制（附件 / 文件）。返回 (状态, 内容, 文件名)。"""
        req = urllib.request.Request(url)
        if referer:
            req.add_header("Referer", referer)
        try:
            with self._op.open(req, timeout=timeout) as resp:
                name = ""
                cd = resp.headers.get("Content-Disposition", "")
                m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", cd, re.I)
                if m:
                    name = urllib.parse.unquote(m.group(1))
                return resp.status, resp.read(), name
        except urllib.error.HTTPError as e:
            return e.code, e.read(), ""
        except Exception as e:
            log.warning("下载失败 %s：%s", url, e)
            return 0, b"", ""


# ---------------------------------------------------------------- 自测

def main() -> int:
    import sys

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    logging.basicConfig(level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    print("=" * 68)
    print("  HTTP 会话自测")
    print("=" * 68)

    print(f"  已保存会话 : {'有' if has_saved_session() else '无'}")
    print(f"  会话文件   : {SESSION_FILE}")

    s = HttpSession()
    s.open()
    print(f"  登录状态   : {'[OK]' if s.logged_in else '[X]'}")
    if s.last_error:
        print(f"  错误       : {s.last_error}")

    if not s.logged_in:
        return 1

    page = s.page()
    page.goto(f"{BASE}/student/classes")
    html = page.content()
    print(f"  /student/classes → {len(html)} 字节")

    import re as _re

    ids = sorted(set(_re.findall(r"/student/classes/(\d+)", html)))
    print(f"  课程链接   : {len(ids)} 个 → {ids[:5]}")

    s.close()
    print("  [OK] 自测通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
