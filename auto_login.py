"""自动登录 —— 用保存的凭据自动完成登录。

【重要前提】
    必须使用带 `--disable-blink-features=AutomationControlled` 的浏览器，
    否则 `navigator.webdriver=true` 会被 ManageBac 风控识别，
    表现为「密码正确却提示错误」。

【登录方式】
    1. 在真实 Edge 窗口中用 JS 填入表单并提交（非模拟键鼠，指纹更干净）
    2. 勾选「Remember me for 30 days」，让登录态尽量持久
    3. 成功后立即抓一次数据，这样下次打开小组件就有内容

用法：
    uv run auto_login.py            # 登录并抓取
    uv run auto_login.py --no-fetch # 只登录
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import cdp, config  # noqa: E402

log = logging.getLogger("auto_login")

CRED_FILE = config.ROOT / "credentials.json"
HOME = f"{config.BASE_URL}/student/home"
LOGIN_URL = f"{config.BASE_URL}/login"

# 登录页选择器（来自实测）
SEL_LOGIN = "#login, input[name='login'], input[id*='login']"
SEL_PASSWORD = "#password, input[name='password'], input[type='password']"
SEL_REMEMBER = "input[type='checkbox']"
SEL_SUBMIT = "input[type='submit'], button[type='submit'], button"

# 填表用的 JS（在页面上下文执行，不是模拟键鼠）
FILL_JS = """
(() => {
  const out = {filled: {}, clicked: false, error: ''};

  const pick = (sels) => {
    for (const s of sels) {
      const el = document.querySelector(s);
      if (el) return el;
    }
    return null;
  };

  try {
    const u = pick([%s]);
    const p = pick([%s]);
    if (!u) { out.error = 'login field not found'; return JSON.stringify(out); }
    if (!p) { out.error = 'password field not found'; return JSON.stringify(out); }

    const setVal = (el, v) => {
      const proto = el instanceof HTMLTextAreaElement
        ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
      setter.call(el, v);
      el.dispatchEvent(new Event('input',  {bubbles: true}));
      el.dispatchEvent(new Event('change', {bubbles: true}));
      el.dispatchEvent(new Event('blur',   {bubbles: true}));
    };

    setVal(u, %s);
    out.filled.login = true;
    setVal(p, %s);
    out.filled.password = true;

    // 勾选「记住我」
    const cb = document.querySelector(%s);
    if (cb) {
      if (!cb.checked) cb.click();
      out.filled.remember = cb.checked;
    }

    // 提交
    const btn = pick([%s]);
    if (btn) {
      btn.click();
      out.clicked = true;
    } else {
      const f = u.closest('form');
      if (f) { f.submit(); out.clicked = true; }
    }
  } catch (e) {
    out.error = String(e);
  }
  return JSON.stringify(out);
})()
"""


def load_credentials() -> dict:
    if not CRED_FILE.exists():
        return {}
    try:
        d = json.loads(CRED_FILE.read_text(encoding="utf-8"))
        return d.get("_managebac", {}) or {}
    except Exception as e:
        log.warning("读取凭据失败：%s", e)
        return {}


# ============================ 登录错误识别 ============================
#
# 【重要教训】服务端的登录失败提示**不在** .login-flash 里，而是：
#     div.alert.alert-danger > p.alert-heading + div.alert-content
# 早期版本只找 .login-flash → 永远读不到原因 → 误以为「被风控」→
# 反复重试 → 每次都重置锁定计时器 → 账号被锁得更久。
#
# 现在严格区分三种情况，并且：
#   locked / bad_credentials  → 立即返回，**不再重试**


class LoginError:
    """登录失败原因。kind ∈ {locked, bad_credentials, other, none}"""

    __slots__ = ("kind", "detail")

    def __init__(self, kind: str = "none", detail: str = "") -> None:
        self.kind = kind
        self.detail = detail

    def __bool__(self) -> bool:
        return self.kind != "none"

    def __repr__(self) -> str:
        return f"LoginError({self.kind!r}, {self.detail!r})"


_READ_ERR_JS = r"""
(() => {
  const grab = (sel) => {
    const el = document.querySelector(sel);
    return el ? (el.innerText || '').replace(/\s+/g, ' ').trim() : '';
  };
  // 1) 标准 Bootstrap alert（服务端真实报错就在这里）
  let alert = '';
  for (const el of document.querySelectorAll('.alert-danger, .alert-warning, [role=alert]')) {
    const t = (el.innerText || '').replace(/\s+/g, ' ').trim();
    if (t) { alert = t; break; }
  }
  // 2) 字段级错误（simple_form 的 invalid-feedback / error 说明）
  const fields = [];
  for (const el of document.querySelectorAll('.invalid-feedback, .form-group.error .error, .help-block, .field_with_errors')) {
    const t = (el.innerText || '').replace(/\s+/g, ' ').trim();
    if (t) fields.push(t);
  }
  return JSON.stringify({
    alert,
    fields: fields.slice(0, 4),
    flash: grab('.login-flash'),
  });
})()
"""


def read_login_error(page) -> LoginError:
    """读取服务端返回的登录失败原因。"""
    try:
        raw = page.eval(_READ_ERR_JS)
        d = json.loads(raw) if raw else {}
    except Exception as e:
        log.debug("读取登录错误失败：%s", e)
        return LoginError()

    parts = [d.get("alert", ""), d.get("flash", ""), *d.get("fields", [])]
    text = " ".join(p for p in parts if p).strip()
    if not text:
        return LoginError()

    low = text.lower()
    if "locked" in low or "lock" in low and "attempt" in low:
        return LoginError("locked", text[:300])
    if ("invalid email or password" in low or "invalid login" in low
            or "incorrect" in low or "wrong password" in low):
        return LoginError("bad_credentials", text[:300])
    if "canceled" in low or "cancel" in low and "account" in low:
        return LoginError("other", text[:300])
    return LoginError("other", text[:300])


def load_credentials_checked() -> tuple[dict, str]:
    """读凭据并做基本校验，返回 (凭据, 错误说明)。"""
    cred = load_credentials()
    login = (cred.get("login") or "").strip()
    password = (cred.get("password") or "").strip()
    if not login or not password:
        return {}, f"凭据缺失，请检查 {CRED_FILE}"
    return {"login": login, "password": password}, ""


def is_logged_in() -> bool:
    """只读探测登录状态。"""
    if not cdp.is_running(config.BROWSER_PROFILE_DIR):
        return False
    try:
        s = cdp.Session(headless=True)
        try:
            s.open()
            page = s.page()
            page.goto(HOME)
            page.wait_for_timeout(1500)
            return cdp.looks_logged_in(page)
        finally:
            s.close()
    except Exception:
        return False


def ensure_browser_with_clean_fingerprint(headless: bool = True) -> None:
    """确保运行中的浏览器带「消除自动化标记」参数。

    若当前实例是旧参数启动的（webdriver=true），先关掉重启。
    headless=False 时用可见窗口 —— 部分站点对无头浏览器有额外检测。
    """
    if cdp.is_running(config.BROWSER_PROFILE_DIR):
        try:
            port = cdp.read_devtools_port(config.BROWSER_PROFILE_DIR)
            conn = cdp.CDP(port)
            try:
                pages = [t for t in conn.targets() if t.get("type") == "page"]
                if pages:
                    t = pages[0]
                    pg = cdp.Page(conn, conn.attach(t["targetId"]), t["targetId"])
                    wd = pg.eval("navigator.webdriver")
                    if wd is False:
                        log.info("现有浏览器指纹干净（webdriver=false），复用")
                        return
                    log.info("现有浏览器 webdriver=%s，需要重启以获得干净指纹", wd)
            finally:
                conn.close()
        except Exception as e:
            log.warning("指纹检查失败：%s", e)

        cdp.close_profile_browser(config.BROWSER_PROFILE_DIR, timeout=20)
        time.sleep(2)

    log.info("启动干净指纹的 Edge（headless=%s）", headless)
    cdp.launch(config.BROWSER_PROFILE_DIR, headless=headless, url=LOGIN_URL)
    cdp.wait_for_port(config.BROWSER_PROFILE_DIR, timeout=50)
    time.sleep(1.5)


def do_login(verbose: bool = True, headless: bool = True) -> tuple[bool, str]:
    """执行自动登录。返回 (是否成功, 说明)。

    headless=False 时使用可见窗口（更接近真人，通过率更高）。
    """
    cred = load_credentials()
    login = (cred.get("login") or "").strip()
    password = (cred.get("password") or "").strip()
    if not login or not password:
        return False, f"凭据缺失，请检查 {CRED_FILE}"

    if is_logged_in():
        return True, "已经是登录状态"

    ensure_browser_with_clean_fingerprint(headless=headless)

    session = cdp.Session(headless=headless)
    try:
        session.open()
        page = session.page()

        # 确认指纹干净
        wd = page.eval("navigator.webdriver")
        if wd is not False:
            return False, f"浏览器指纹不干净（webdriver={wd}），登录会被风控拦截"

        page.goto(LOGIN_URL)
        page.wait_for_timeout(2200)

        if cdp.looks_logged_in(page):
            return True, "已经在登录后的页面"

        if verbose:
            print(f"  正在填写登录表单（账号 {login}）……")

        js = FILL_JS % (json.dumps([SEL_LOGIN]), json.dumps([SEL_PASSWORD]),
            json.dumps(login), json.dumps(password),
            json.dumps(SEL_REMEMBER), json.dumps([SEL_SUBMIT]),
        )
        raw = page.eval(js)
        info = json.loads(raw) if raw else {}
        if info.get("error"):
            return False, f"填表失败：{info['error']}"
        if not info.get("clicked"):
            return False, "未能提交表单"

        # 等待跳转（登录成功后 URL 会变成 /student/...）
        deadline = time.time() + 45
        while time.time() < deadline:
            time.sleep(1.5)
            try:
                url = page.url or ""
            except Exception:
                continue
            if "/student/" in url:
                page.wait_for_timeout(1200)
                if cdp.looks_logged_in(page):
                    return True, "登录成功"

            #  读取服务端真实提示并分级处理
            if "/login" in url:
                reason = read_login_error(page)
                if reason.kind == "locked":
                    # 绝不能再重试 —— 每次失败都重置锁定计时器
                    return False, f"账号被临时锁定：{reason.detail}"
                if reason.kind == "bad_credentials":
                    return False, f"账号或密码不正确：{reason.detail}"
                if reason.kind == "other":
                    return False, f"登录被拒：{reason.detail}"

        return False, "等待跳转超时（既没成功，也没有明确报错）"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    finally:
        session.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-fetch", action="store_true", help="只登录，不抓取")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--headful", action="store_true",
                    help="用可见窗口登录（无头被拒时使用）")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.FileHandler(config.LOG_DIR / "widget.log",
                                     encoding="utf-8")],
    )
    config.ensure_dirs()

    if not args.quiet:
        print("=" * 68)
        print("  ManageBac 自动登录")
        print("=" * 68)

    headless = not args.headful
    ok, msg = do_login(verbose=not args.quiet, headless=headless)

    #  只在「指纹不干净」这种可修复的情况下才重试有头模式。
    #   账号被锁 / 密码错误 → 绝不重试（每次失败都重置锁定计时器）。
    if not ok and headless and "指纹" in msg:
        if not args.quiet:
            print("  指纹不干净，改用可见窗口重试……")
        ok, msg = do_login(verbose=not args.quiet, headless=False)

    if not args.quiet:
        print(f"  {'[OK]' if ok else '[X] '} {msg}")

    if not ok and ("锁定" in msg or "锁" in msg):
        if not args.quiet:
            print()
            print("  ┌────────────────────────────────────────────────────────┐")
            print("  │  账号已被临时锁定，请不要再尝试登录。                  │")
            print("  │  等待 15~30 分钟后会自动解锁。                         │")
            print("  │  期间每次尝试都会重置计时器，只会拖得更久。            │")
            print("  └────────────────────────────────────────────────────────┘")

    if ok and not args.no_fetch:
        if not args.quiet:
            print("  正在抓取数据（这样下次打开就有内容）……")
        from app import pipeline

        pipeline.load_from_cache()
        pipeline.refresh_sync()
        s = pipeline.snapshot()
        if not args.quiet:
            print(f"  {s['message']}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
