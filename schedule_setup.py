"""课表登录 —— 自动填表，你只需点一下「登录」。

比旧版 `schedule_login.py` 省事的地方：
    1. 打开页面后**自动填入**手机号与密码（你只需点登录按钮）
    2. 检测到登录成功后**自动保存 token**（之后开机预热就不需要浏览器了）
    3. 自动跳到课表页，把数据加载出来
    4. 全程自动，登录成功即退出（不用手动按回车）

用法：
    uv run schedule_setup.py

完成后：`data/schedule_token.json` 会存下 token，
        以后 `warmup.py` / 小组件都能直接抓课表（不再需要浏览器）。
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from app import cdp, config, schedule  # noqa: E402

URL = "https://yly.seiue.com/"
CRED_FILE = config.ROOT / "credentials.json"


def load_cred() -> dict:
    try:
        d = json.loads(CRED_FILE.read_text(encoding="utf-8"))
        return d.get("_schedule", {}) or {}
    except Exception:
        return {}


def ts() -> str:
    return time.strftime("%H:%M:%S")


# ---------------------------------------------------------------- 探测登录框

FIND_LOGIN_INPUTS = r"""
(() => {
  const out = {inputs: [], buttons: [], url: location.href, title: document.title,
               hasToken: false, text: ''};
  try {
    const raw = localStorage.getItem('persist:root');
    if (raw) {
      const root = JSON.parse(raw);
      const sess = root.session ? JSON.parse(root.session) : null;
      const t = sess && sess.oAuthToken;
      out.hasToken = !!(t && t.accessToken);
      out.reflectionId = t ? t.activeReflectionId : null;
    }
  } catch(e) {}

  const vis = (el) => {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    return r.width > 2 && r.height > 2 && st.visibility !== 'hidden'
           && st.display !== 'none' && st.opacity !== '0';
  };

  document.querySelectorAll('input').forEach((el, i) => {
    if (!vis(el)) return;
    out.inputs.push({
      i: i,
      type: el.type, inputmode: el.inputMode,
      name: el.name || '', id: el.id || '',
      placeholder: el.placeholder || '',
      autocomplete: el.autocomplete || '',
      cls: (el.className || '').toString().slice(0, 90),
      valueLen: (el.value || '').length,
    });
  });

  document.querySelectorAll('button, [role=button], .btn, [class*=button]').forEach((el) => {
    if (!vis(el)) return;
    const tx = (el.innerText || '').replace(/\s+/g, ' ').trim();
    if (tx && tx.length < 24) out.buttons.push(tx);
  });

  out.text = (document.body.innerText || '').replace(/\s+/g, ' ').slice(0, 400);
  return JSON.stringify(out);
})()
"""


def inspect(page) -> dict:
    try:
        raw = page.eval(FIND_LOGIN_INPUTS)
        return json.loads(raw) if raw else {}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------- 自动填表

FILL = r"""
(() => {
  const login = %s, password = %s;
  const out = {filledUser: false, filledPass: false, clicked: false, error: ''};

  const vis = (el) => {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    return r.width > 2 && r.height > 2 && st.visibility !== 'hidden'
           && st.display !== 'none';
  };

  // React/Vue 受控组件：必须用原生 setter + 触发事件，否则值会被重置
  const setVal = (el, v) => {
    const proto = el instanceof HTMLTextAreaElement
      ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const desc = Object.getOwnPropertyDescriptor(proto, 'value');
    if (desc && desc.set) desc.set.call(el, v); else el.value = v;
    el.dispatchEvent(new Event('input',  {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
    el.dispatchEvent(new Event('blur',   {bubbles: true}));
  };

  const inputs = [...document.querySelectorAll('input')].filter(vis);

  const isUser = (el) => {
    const s = ((el.type||'') + ' ' + (el.name||'') + ' ' + (el.placeholder||'')
              + ' ' + (el.id||'') + ' ' + (el.autocomplete||'')).toLowerCase();
    return el.type === 'tel' || el.inputMode === 'numeric'
| /phone|mobile|user|account|login|tel|手机|账号|用户/.test(s);
  };
  const isPass = (el) => el.type === 'password'
| /password|passwd|pwd|密码/.test(((el.name||'')+(el.placeholder||'')).toLowerCase());

  let p = inputs.find(isPass) || null;
  // 用户名：优先非密码框
  let u = inputs.find(el => el !== p && isUser(el)) || null;

  if (p) { setVal(p, password); out.filledPass = true; }
  if (u) { setVal(u, login);     out.filledUser = true; }

  // 点「登录」
  const btns = [...document.querySelectorAll('button, [role=button], .btn')]
                 .filter(vis);
  const hit = btns.find(b =>
      /^(登\s*录|登录|Login|Sign in|Sign In|立即登录|确定)$/i
        .test((b.innerText||'').replace(/\s+/g,'').trim()));
  if (hit) { hit.click(); out.clicked = true; }

  return JSON.stringify(out);
})()
"""


def main() -> int:
    config.ensure_dirs()
    cred = load_cred()
    login_id = (cred.get("login") or "").strip()
    password = (cred.get("password") or "").strip()

    print("=" * 72)
    print("  课表一次性设置")
    print("=" * 72)
    print(f"  网站     : {URL}")
    print(f"  账号     : {login_id or '（未配置）'}")
    print(f"  数据目录 : {schedule.PROFILE}")
    print()

    # 已经存过 token？直接验证
    saved = schedule.load_token()
    if saved:
        print(f"  [i] 已有保存的 token（{schedule.token_age_text()}），先验证一下……")
        sch = schedule.fetch_schedule()
        if sch.ready and sch.lessons:
            print(f"  [OK] token 仍然有效 —— 抓到 {len(sch.lessons)} 节课")
            print("       无需重新登录，设置已完成。")
            return 0
        print(f"  [!] token 已失效（{sch.error}），需要重新登录。\n")

    if not login_id or not password:
        print(f"  [X] 请在 {CRED_FILE} 里填好 _schedule 的 login / password")
        return 1

    # 打开浏览器
    if cdp.is_running(schedule.PROFILE):
        print("  [i] 课表浏览器已在运行，复用。")
    else:
        print("  正在打开 Edge 窗口……")
        cdp.launch(schedule.PROFILE, headless=False, url=URL)
    cdp.wait_for_port(schedule.PROFILE, timeout=50)
    time.sleep(2)

    s = cdp.Session(headless=False, profile_dir=schedule.PROFILE)
    s.open()
    page = s.page()

    # 确保在登录页
    try:
        if "seiue" not in (page.url or ""):
            page.goto(URL)
        page.wait_for_timeout(3500)
    except Exception as e:
        print(f"  [!] 导航异常：{e}")

    print()
    info = inspect(page)
    if info.get("hasToken"):
        print("  [OK] 浏览器里已是登录状态！")
    else:
        # 自动填表
        print(f"[{ts()}] 正在自动填写登录表单……")
        js = FILL % (json.dumps(login_id), json.dumps(password))
        try:
            raw = page.eval(js)
            res = json.loads(raw) if raw else {}
        except Exception as e:
            res = {"error": str(e)}

        ok_user = res.get("filledUser")
        ok_pass = res.get("filledPass")
        clicked = res.get("clicked")
        print(f"         账号框 {'[OK]' if ok_user else '[X]'}    "
              f"密码框 {'[OK]' if ok_pass else '[X]'}    "
              f"已点登录 {'[OK]' if clicked else '[X]'}")

        if not (ok_user and ok_pass):
            d = inspect(page)
            print("\n  [!] 没能自动找到输入框。页面上的输入框如下：")
            for it in d.get("inputs", [])[:10]:
                print(f"      type={it['type']:9} name={it['name'][:16]:16} "
                      f"placeholder={it['placeholder'][:22]}")
            print(f"      按钮: {d.get('buttons', [])[:10]}")
            print("\n  → 请在那个 Edge 窗口里**手动**登录（程序会继续等待）。")
        elif not clicked:
            print("  → 已填好，请在窗口里点一下「登录」按钮。")

        print(f"\n[{ts()}] 等待登录完成（最多 5 分钟）……")

    # 轮询等待登录成功
    deadline = time.time() + 300
    last_note = 0.0
    ok = False
    while time.time() < deadline:
        time.sleep(3)
        d = inspect(page)
        if d.get("hasToken"):
            ok = True
            break
        # 偶尔提示，避免看起来卡住
        if time.time() - last_note > 30:
            last_note = time.time()
            left = int(deadline - time.time())
            print(f"        [{ts()}] 仍在等待……（剩余 {left // 60} 分 {left % 60} 秒）")

    if not ok:
        print(f"\n  [X] 超时未检测到登录。")
        print(f"      你可以稍后重新运行：uv run schedule_setup.py")
        return 1

    print(f"\n  [OK] 检测到登录成功！正在保存 token……")
    sess = schedule.read_session()
    if sess.get("error"):
        print(f"  [!] 读取会话失败：{sess['error']}")
        return 1

    schedule.save_token(sess)
    print(f"      用户       : {sess.get('user_name') or '（未知）'}")
    print(f"      用户标识   : {sess.get('reflection_id')}")
    print(f"      token 文件 : {schedule.TOKEN_FILE}")

    # 顺手抓一次课表验证
    print(f"\n[{ts()}] 验证课表抓取……")
    sch = schedule.fetch_schedule()
    if sch.ready:
        print(f"  [OK] 抓到 {len(sch.lessons)} 节课")
        from collections import Counter

        days = Counter(l.day for l in sch.lessons)
        print(f"      覆盖 {len(days)} 天")
        for d0 in sorted(days)[:4]:
            print(f"        {d0}  {days[d0]} 节")
    else:
        print(f"  [!] 课表抓取未成功：{sch.error}")

    print()
    print("=" * 72)
    print("  [OK] 设置完成！")
    print("=" * 72)
    print("  以后开机预热会自动带上课表，不需要再开浏览器。")
    print("  这个 Edge 窗口可以关掉了。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
