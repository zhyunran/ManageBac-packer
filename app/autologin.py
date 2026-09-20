"""自动续期 —— 在后台续期登录态，减少手动操作。

【用户要求】
    「所有内容集中处理，不要出现无法打开课表、
      让我自己运行某个 python 程序这种。
      要么就弹出网页让我登录，最好是你自己解决。」

【两条链路的登录态】

    ① ManageBac（学校系统）
       · 登录方式：普通表单 POST（纯 HTTP 就行）
       · 有效期：Cookie 约 30 天（前提是勾了「记住我」）
       · 续期难度：低 —— 有账号密码就能全自动重登

    ② 课表（yly.seiue.com，另一个学校系统）
       · 登录方式：React SPA，有 OAuth（`POST /chalk/oauth/tokens`
         实测报服务端 500，没法纯 HTTP 登）
       · 有效期：JWT，一般 7~30 天
       · 续期难度：高 —— **必须走浏览器**，用 JS 往 React 输入框里填值

【本模块干的事】

    1. **定期体检**：每隔一段时间检查两个会话还能不能用
    2. **自动续期（无头）**：
       · ManageBac → 纯 HTTP 重登（快、静默）
       · 课表 → 启动**无头** Edge + CDP，用 JS 填表登录，读出 JWT
    3. **兜底弹窗**：无头登录失败时，自动弹出**可见**浏览器窗口，
       把账号密码预先填好，只剩点「登录」一下
    4. **绝不死循环**：失败后按指数退避，绝不在短时间内重复提交
       （ 账号连续 5 次失败会被锁，这是踩过的坑）

【为什么不是「后台定时任务」】

    和晚报（digest.py）同一个思路：组件不是 24 小时常驻的。
    所以本模块提供的是一个**「需要时才跑」的函数**，
    由 pipeline 在每次抓取时顺手调用 —— 电脑关了再开也能自动补上。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config, httpclient

log = logging.getLogger("autologin")

STATE_FILE = config.DATA_DIR / "autologin.json"

_lock = threading.RLock()

# 同一时刻只允许一个登录流程在跑
_running: set[str] = set()

# 会话剩余有效期低于这个值就续期（秒）
RENEW_MARGIN = 3 * 86400          # 3 天


# ══════════════════════════════════════════════════════════════════
#  状态文件
# ══════════════════════════════════════════════════════════════════

def _read_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_state(**kw) -> dict:
    with _lock:
        st = _read_state()
        st.update(kw)
        st["at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            config.ensure_dirs()
            tmp = STATE_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            tmp.replace(STATE_FILE)
        except Exception as e:
            log.warning("写入自动登录状态失败：%s", e)
        return st


def read_state() -> dict:
    return _read_state()


# ══════════════════════════════════════════════════════════════════
#  退避（ 防止锁号）
# ══════════════════════════════════════════════════════════════════

def _last_attempt(key: str) -> float:
    return float(_read_state().get(f"{key}_last_attempt") or 0)


def _backoff_ok(key: str) -> tuple[bool, int]:
    """还能再试吗？

    连续失败次数越多，等得越久（指数退避）。
     这是「防止触发风控锁号」的关键 —— 绝不能短时间内反复提交。
    """
    st = _read_state()
    fails = int(st.get(f"{key}_fails") or 0)
    last = _last_attempt(key)

    if fails == 0:
        gap = config.AUTO_LOGIN_MIN_GAP          # 10 分钟
    else:
        # 10 分钟 → 20 → 40 → 80 → 160 → 320 → 封顶 60 分钟
        gap = min(config.AUTO_LOGIN_MIN_GAP * (2 ** min(fails, 6)),
                  60 * 60)

    waited = time.time() - last
    if waited >= gap:
        return True, 0
    return False, int(gap - waited)


def _record_result(key: str, ok: bool, msg: str = "") -> None:
    st = _read_state()
    fails = 0 if ok else int(st.get(f"{key}_fails") or 0) + 1
    _write_state(**{
        f"{key}_last_attempt": time.time(),
        f"{key}_fails": fails,
        f"{key}_last_ok": ok,
        f"{key}_msg": msg,
        f"{key}_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })


# ══════════════════════════════════════════════════════════════════
#  ① ManageBac —— 纯 HTTP 重登
# ══════════════════════════════════════════════════════════════════

def mb_session_ok() -> bool:
    """只读探测：ManageBac 会话还有效吗（**只走纯 HTTP，不启动浏览器**）。

     为什么不复用 warmup.probe_login()：
      那个函数在 HTTP 探测失败时会**退回浏览器**（启动无头 Edge），
      而这里只是做一次「体检」，不应该有那么大的副作用
      —— 实测会导致：组件启动时莫名拉起一个 Edge，用户看着很困惑。

      本函数只发一个 HTTP 请求：
        · 429（限速）    → 保守认为「还有效」，别去重登（重登会更糟）
        · 302 → /login   → 真的失效了
        · 200            → 有效
    """
    try:
        if not httpclient.has_saved_session():
            return False

        s = httpclient.HttpSession(auto_login_on_demand=False)
        try:
            s.open()
            return bool(s.logged_in)
        finally:
            s.close()
    except Exception as e:
        log.warning("探测 ManageBac 登录态失败：%s", e)
        return False


def renew_managebac(silent: bool = True) -> tuple[bool, str]:
    """重新登录 ManageBac（纯 HTTP，无窗口）。

     安全：`auto_login` 内部已保证「识别到锁定/密码错误就立即停止」，
      绝不盲目重试。
    """
    key = "mb"
    if key in _running:
        return True, "已有登录流程在进行"
    ok_gap, wait = _backoff_ok(key)
    if not ok_gap:
        m = wait // 60 + 1
        return False, f"为避免触发风控，{m} 分钟后再试"

    _running.add(key)
    try:
        import auto_login as al

        log.info("开始自动重新登录 ManageBac …")
        # 先试无头（完全静默）；失败再试有头（更接近真人，通过率更高）
        ok, msg = al.do_login(verbose=False, headless=True)
        if not ok:
            # 只在失败原因不是「锁号/密码错」时才换有头重试
            #  锁号/密码错绝不能重试（每次失败都重置锁定计时器）
            if any(k in (msg or "") for k in ("锁定", "锁", "不正确", "凭据缺失")):
                _record_result(key, False, msg)
                log.warning("ManageBac 自动登录失败（不重试）：%s", msg)
                return False, msg

            log.info("无头登录未成功（%s），改用可见窗口重试一次", msg)
            ok, msg = al.do_login(verbose=False, headless=False)

        _record_result(key, ok, msg)
        if ok:
            log.info("ManageBac 自动登录成功")
        else:
            log.warning("ManageBac 自动登录失败：%s", msg)
        return ok, msg
    except Exception as e:
        _record_result(key, False, str(e))
        log.warning("ManageBac 自动登录异常：%s", e)
        return False, f"{type(e).__name__}: {e}"
    finally:
        _running.discard(key)


# ══════════════════════════════════════════════════════════════════
#  ② 课表 —— 必须走浏览器（SPA + OAuth）
# ══════════════════════════════════════════════════════════════════

# 填表 JS：往 React 受控组件里写值，必须用原生 setter + 触发事件
_SCHEDULE_FILL_JS = r"""
(() => {
  const LOGIN = %s, PASS = %s;
  const out = {inputs: 0, user: false, pass: false, clicked: false, error: ''};

  const vis = (el) => {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    return r.width > 2 && r.height > 2
        && st.visibility !== 'hidden' && st.display !== 'none'
        && st.opacity !== '0';
  };

  const setVal = (el, v) => {
    const proto = el instanceof HTMLTextAreaElement
      ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
    setter.call(el, v);
    el.dispatchEvent(new Event('input',  {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
  };

  try {
    const all = [...document.querySelectorAll('input')].filter(vis);
    out.inputs = all.length;
    if (!all.length) { out.error = 'no input'; return JSON.stringify(out); }

    // 找密码框（type=password）和账号框（密码框之前那个）
    let pEl = all.find(e => e.type === 'password');
    let uEl = null;
    if (pEl) {
      const i = all.indexOf(pEl);
      // 往前找第一个看起来像账号的框
      for (let k = i - 1; k >= 0; k--) {
        const t = (all[k].type || '').toLowerCase();
        const im = (all[k].inputMode || '').toLowerCase();
        if (t === 'text' || t === 'tel' || t === 'email'
| im === 'numeric' || im === 'tel' || im === 'email') {
          uEl = all[k]; break;
        }
      }
      // 再往前没找到就用第一个可见输入框
      if (!uEl && i > 0) uEl = all[0];
    } else {
      // 没密码框 —— 可能分两步（先填账号，下一页再输密码）
      uEl = all[0];
    }

    if (uEl) { setVal(uEl, LOGIN);  out.user = true; }
    if (pEl) { setVal(pEl, PASS);   out.pass = true; }

    // 找提交按钮
    const btns = [...document.querySelectorAll('button, [role=button], input[type=submit], .btn'
    )].filter(vis);
    const btn = btns.find(b => {
      const t = ((b.innerText || b.value || '') + '').trim();
      return /登\s*录|sign\s*in|log\s*in|下一步|next|确定|提交/i.test(t);
    }) || btns[0];

    if (btn) { btn.click(); out.clicked = true; }
    else if (uEl && uEl.form) { uEl.form.submit(); out.clicked = true; }
    else { out.error = 'no submit button'; }
  } catch (e) {
    out.error = String(e);
  }
  return JSON.stringify(out);
})()
"""

# 读 token JS
_SCHEDULE_READ_JS = r"""
(() => {
  const out = {token: '', rid: '', name: '', sem: '', error: ''};
  try {
    const raw = localStorage.getItem('persist:root');
    if (!raw) { out.error = 'no persist:root'; return JSON.stringify(out); }
    const root = JSON.parse(raw);
    const sess = root.session ? JSON.parse(root.session) : null;
    const t = sess && sess.oAuthToken;
    if (t && t.accessToken) {
      out.token = t.accessToken;
      out.rid = String(t.activeReflectionId || '');
      out.name = (sess.currentUser && sess.currentUser.name) || '';
    } else {
      out.error = 'no accessToken';
    }
    try {
      const s = localStorage.getItem('g-p-semester-selector');
      if (s && /^\d+$/.test(s)) out.sem = s;
    } catch(e) {}
  } catch (e) { out.error = String(e); }
  return JSON.stringify(out);
})()
"""


def schedule_token_ok() -> bool:
    """课表 token 还能用吗（真的发一个请求验证，不只看文件存在）。

     重要：课表是**另一个**站点（api.seiue.com），和 ManageBac 的
      限速冷却**互不影响**。所以这里先检查课表自己的请求是否真的发出去了，
      避免被 ManageBac 的冷却状态误判成「token 失效」。
    """
    try:
        from . import schedule

        sess = schedule.load_token()
        if not sess.get("access_token") or not sess.get("reflection_id"):
            return False

        # 用最小请求验证 token 是否还有效
        # （极小区间，几乎不产生负载）
        status, text = schedule.api_get(f"/chalk/calendar/personals/{sess['reflection_id']}/events",
            sess["access_token"],
            {"start_time": "2000-01-01 00:00:00",
             "end_time": "2000-01-02 00:00:00"},
            timeout=20,
        )
        if status == 200:
            return True
        if status in (401, 403):
            log.info("课表 token 已失效（HTTP %s）", status)
            return False
        if status == 0:
            # 请求根本没发出去（网络问题 / 被本地的冷却挡了）
            # → 保守认为 token 还有效，别贸然重登
            log.info("课表请求未能发出（%s），保守认为 token 仍有效",
                     (text or "")[:80])
            return True
        # 其他状态码也保守处理
        log.info("课表接口返回 %s，保守认为 token 仍有效", status)
        return True
    except Exception as e:
        log.warning("探测课表 token 失败：%s", e)
        return True          # 网络问题时保守处理


def _schedule_credentials() -> dict:
    try:
        cred = json.loads((config.ROOT / "credentials.json")
                          .read_text(encoding="utf-8"))
        return cred.get("_schedule") or {}
    except Exception:
        return {}


def renew_schedule(headless: bool = True,
                   allow_popup: bool | None = None) -> tuple[bool, str]:
    """重新获取课表 token。

    Args:
        headless: 先用无头浏览器试（不给用户任何打扰）
        allow_popup: 无头失败时是否弹可见窗口
                     None → 用 config.AUTO_LOGIN_ALLOW_POPUP

    Returns:
        (成功?, 说明文字)
    """
    key = "sch"
    if key in _running:
        return True, "已有登录流程在进行"
    ok_gap, wait = _backoff_ok(key)
    if not ok_gap:
        m = wait // 60 + 1
        return False, f"为避免触发风控，{m} 分钟后再试"

    cred = _schedule_credentials()
    login = (cred.get("login") or "").strip()
    pwd = (cred.get("password") or "").strip()
    if not login or not pwd:
        return False, "没有课表账号（credentials.json 里缺 _schedule）"

    if allow_popup is None:
        allow_popup = config.AUTO_LOGIN_ALLOW_POPUP

    _running.add(key)
    try:
        # ── 第一步：无头尝试（完全静默）──
        ok, msg = _schedule_login_browser(login, pwd, headless=True)
        if ok:
            _record_result(key, True, "headless")
            log.info("课表 token 自动续期成功（无头）")
            return True, "已自动续期"

        log.info("课表无头登录未成功（%s）", msg)

        # ── 第二步：弹窗兜底（用户只需点一下）──
        if allow_popup:
            log.info("改用可见窗口，请用户手动完成一次登录")
            ok2, msg2 = _schedule_login_browser(login, pwd, headless=False, wait_manual=True,
            )
            if ok2:
                _record_result(key, True, "popup")
                return True, "已通过弹窗登录"
            msg = msg2 or msg

        _record_result(key, False, msg)
        return False, msg
    except Exception as e:
        _record_result(key, False, str(e))
        log.warning("课表自动登录异常：%s", e)
        return False, f"{type(e).__name__}: {e}"
    finally:
        _running.discard(key)


# ══════════════════════════════════════════════════════════════════
#  JWT 过期判断（ 关键：不能只「读到 token」就算成功）
# ══════════════════════════════════════════════════════════════════

def jwt_expired(token: str, margin: int = 3600) -> bool:
    """解码 JWT 的 exp 声明，判断是否已过期（或即将过期）。

     为什么必须做这一步：
      浏览器 localStorage 里存的是**上次登录的 token**。
      如果它已经过期，直接读出来会拿到一个「看起来有、实际没用」的 token
      —— 实测就踩过这个坑：续期 2.2 秒就「成功」，但一用就 401。

    Args:
        margin: 提前多少秒算「即将过期」（默认 1 小时）
    """
    try:
        parts = (token or "").split(".")
        if len(parts) < 2:
            return True                     # 不是 JWT → 当作过期

        # base64url 解码 payload（补上 padding）
        import base64
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        raw = base64.urlsafe_b64decode(payload)
        data = json.loads(raw.decode("utf-8"))

        exp = data.get("exp")
        if not exp:
            return False                    # 没写过期时间 → 当作长期有效
        return time.time() >= (float(exp) - margin)
    except Exception as e:
        log.info("JWT 解析失败（%s），当作需要重新登录", e)
        return True


def _schedule_login_browser(login: str, pwd: str, headless: bool,
                            wait_manual: bool = False) -> tuple[bool, str]:
    """用浏览器完成一次课表登录，成功后把 token 存下来。

     关键设计：
      · 用 `--disable-blink-features=AutomationControlled` 净化指纹
      · 用 JS 往 React 输入框写值（不是模拟键鼠），不容易被识别
      · **先检查 JWT 是否过期** —— 过期就清掉 localStorage 强制重新登录，
        否则会「读到旧 token 就当成功」，实际一用就 401（踩过这个坑）
      · 无头模式失败 → 返回失败，由调用方决定要不要弹窗
      · `wait_manual=True` 时会**等**用户手动完成（最多 5 分钟）
    """
    from . import cdp, schedule as sch

    profile = sch.PROFILE
    proc = None
    try:
        # 已有浏览器在跑就复用它
        already = cdp.is_running(profile)
        if not already:
            proc = cdp.launch(profile, headless=headless,
                              url="https://yly.seiue.com/")
        port = cdp.wait_for_port(profile, timeout=60)
        conn = cdp.CDP(port)

        try:
            # 找到一个 seiue 的标签页；没有就打开一个
            pages = [t for t in conn.targets()
                     if t.get("type") == "page"
                     and "seiue" in (t.get("url") or "").lower()]
            if pages:
                t = pages[0]
            else:
                tid, _ = conn.create("https://yly.seiue.com/")
                t = {"targetId": tid, "url": "https://yly.seiue.com/"}

            pg = cdp.Page(conn, conn.attach(t["targetId"]), t["targetId"])
            time.sleep(2.0)

            # ── ① 先看 localStorage 里有没有「还能用」的 token ──
            raw = pg.eval(_SCHEDULE_READ_JS)
            got = json.loads(raw) if raw else {}
            tok = got.get("token") or ""

            if tok and got.get("rid"):
                if not jwt_expired(tok):
                    log.info("浏览器里的 token 仍未过期，直接复用")
                    return _save_schedule_token(got)
                log.info("浏览器里的 token 已过期（或被拒），清掉它重新登录")
                #  清掉过期的会话，避免页面「看起来已登录」但实际失效
                try:
                    pg.eval("""
                      (() => {
                        try {
                          localStorage.removeItem('persist:root');
                          sessionStorage.clear();
                          return 'cleared';
                        } catch(e) { return String(e); }
                      })()
                    """)
                except Exception:
                    pass
                # 刷新页面，回到登录状态
                try:
                    pg.goto("https://yly.seiue.com/")
                except Exception:
                    pass
                time.sleep(3.0)

            # ── ② 填表 ──
            js = _SCHEDULE_FILL_JS % (json.dumps(login), json.dumps(pwd)
            )
            raw = pg.eval(js)
            filled = json.loads(raw) if raw else {}
            log.info("课表填表结果：%s", filled)

            if not filled.get("user"):
                return False, f"找不到登录输入框（{filled.get('error') or '?'}）"

            # ── ③ 等 token 出现（并且是真的没过期的）──
            deadline = time.time() + (300 if wait_manual else 60)
            last_reject = ""
            while time.time() < deadline:
                time.sleep(2.0)
                try:
                    raw = pg.eval(_SCHEDULE_READ_JS)
                    got = json.loads(raw) if raw else {}
                except Exception:
                    got = {}

                tok = got.get("token") or ""
                if tok and got.get("rid"):
                    if not jwt_expired(tok):
                        return _save_schedule_token(got)
                    last_reject = "读到的 token 仍然是过期的"
                elif got.get("error"):
                    last_reject = got["error"]

            # ── ④ 超时 ──
            hint = ""
            try:
                hint = (pg.inner_text() or "")[:160].replace("\n", " ")
            except Exception:
                pass
            why = last_reject or "没看到登录成功"
            return False, f"等待登录超时（{why}）。页面提示：{hint or '(无)'}"
        finally:
            conn.close()
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    finally:
        # 无头模式自己开的浏览器要关掉；有头模式留给用户（他可能还要看）
        if proc is not None and headless:
            try:
                cdp.terminate(proc)
            except Exception:
                pass


def _save_schedule_token(got: dict) -> tuple[bool, str]:
    from . import schedule as sch

    sess = {
        "access_token": got.get("token") or "",
        "reflection_id": str(got.get("rid") or ""),
        "user_name": got.get("name") or "",
        "semester_id": got.get("sem") or "",
    }
    if not sess["access_token"] or not sess["reflection_id"]:
        return False, "读到的会话不完整"
    sch.save_token(sess)
    return True, "已保存"


# ══════════════════════════════════════════════════════════════════
#  统一入口
# ══════════════════════════════════════════════════════════════════

def ensure_all(reason: str = "") -> dict:
    """体检 + 按需续期。**在抓取流程里顺手调用**。

    Returns:
        {"mb": {...}, "schedule": {...}}  各自的检查/续期结果
    """
    out: dict = {}

    # ── ManageBac ──
    try:
        if mb_session_ok():
            out["mb"] = {"ok": True, "action": "none", "msg": "会话有效"}
        else:
            t0 = time.time()
            ok, msg = renew_managebac()
            out["mb"] = {
                "ok": ok,
                "action": "renewed" if ok else "failed",
                "msg": msg,
                "took": round(time.time() - t0, 1),
            }
    except Exception as e:
        out["mb"] = {"ok": False, "action": "error", "msg": str(e)}

    # ── 课表 ──
    try:
        if schedule_token_ok():
            out["schedule"] = {"ok": True, "action": "none", "msg": "token 有效"}
        else:
            t0 = time.time()
            ok, msg = renew_schedule()
            out["schedule"] = {
                "ok": ok,
                "action": "renewed" if ok else "failed",
                "msg": msg,
                "took": round(time.time() - t0, 1),
            }
    except Exception as e:
        out["schedule"] = {"ok": False, "action": "error", "msg": str(e)}

    if reason:
        out["reason"] = reason
    _write_state(last_check=out)
    return out


def status() -> dict:
    """给界面用的概要。"""
    st = _read_state()
    mb_fails = int(st.get("mb_fails") or 0)
    sch_fails = int(st.get("sch_fails") or 0)

    # 课表 token 年龄
    try:
        from . import schedule as sch
        tok = sch.load_token()
        sch_age = (time.time() - float(tok.get("saved_at") or 0)) if tok else None
    except Exception:
        sch_age = None

    return {
        "ok": True,
        "mb_fails": mb_fails,
        "schedule_fails": sch_fails,
        "schedule_token_age_h": (round(sch_age / 3600, 1)
                                 if sch_age is not None else None),
        "last": st.get("last_check") or {},
        "at": st.get("at") or "",
        "busy": bool(_running),
    }
