"""Teams 弹窗守卫 —— 让「是否打开桌面版」这类弹窗不再卡住程序。

【踩过的坑】
    Teams 网页会时不时弹两样东西：

    ① **系统级弹窗**：「是否允许此网站打开 Microsoft Teams?」
       —— 这不是网页元素，是 Windows 的协议处理器询问框。
       一旦弹出来，浏览器窗口被模态挡住；更要命的是 Teams 会顺势
       跳到一个 ``dl/launcher/launcher.html`` 中转页，主界面就没了。
       程序于是读不到任何元素，看起来就是「卡死」。

    ② **页内横幅 / 弹层**：「下载桌面版」「启用桌面通知」
       —— 这些虽然能点掉，但会顶掉布局、遮挡元素，读数据容易出错。

【两道防线】
    防线一（治本，写配置）：往浏览器 profile 的 ``Preferences`` 里塞
        ``protocol_handler.excluded_schemes``，把 Teams 相关协议拉黑。
        该键只在浏览器**启动时**读取，所以必须在启动前写。
        写成 ``true`` 表示「永不询问、永不打开」。
      注：写这个文件必须在 Edge 关闭状态下做，否则退出时会被覆盖。

    防线二（兜底，跑起来管）：页面里定期「清场」——
        发现跳到了中转页就导航回来；发现横幅 / 弹层就点掉。
        Windows 原生询问框关不了，但中转页能救回来，这就够了。
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)

#  Teams 相关协议。ms-teams 是 Windows 版注册的主协议，
#  msteams 是 macOS / 新版网页用的；其余几个是 Teams 内部调起的辅助协议。
TEAMS_SCHEMES = (
    "ms-teams",
    "msteams",
    "msteams-call",
    "ms-teams-call",
    "teams",
    "microsoft-teams",
    "ms-outlook",       # 有些学校工作流里会串到 Outlook
)

#  「打开桌面版」中转页的 URL 特征。
#  Teams 会把链接包成 dl/launcher/launcher.html?url=...，点一下就跳应用。
LAUNCHER_HINTS = ("dl/launcher/", "launcher/launcher.html", "/dl/launcher")

#  中转页的标题（中英都要，账号语言可能是任意一种）
LAUNCHER_TITLES = ("加入对话", "join conversation", "正在打开", "opening")


# ══════════════════════════════════════════════════════════
#  防线一：写 profile 配置
# ══════════════════════════════════════════════════════════

def harden_profile(profile_dir: Path) -> dict:
    """把 Teams 协议拉黑，浏览器启动时就不会再询问。

    必须在 Edge **没在跑** 的时候调用 —— 否则浏览器退出时会用
    内存里的旧配置覆盖回去，白写。

    返回 ``{ok, why?, schemes}``。
    """
    profile_dir = Path(profile_dir)
    pref_file = profile_dir / "Default" / "Preferences"

    if not pref_file.parent.exists():
        profile_dir.mkdir(parents=True, exist_ok=True)
        try:
            pref_file.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return {"ok": False, "why": f"建目录失败：{e}"}

    data: dict = {}
    if pref_file.exists():
        try:
            raw = pref_file.read_text(encoding="utf-8", errors="ignore")
            data = json.loads(raw) if raw.strip() else {}
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            #  文件坏了也不要紧，重建一份最小配置即可 ——
            #  Edge 会用默认值补齐缺失的键。
            data = {}

    ph = data.setdefault("protocol_handler", {})
    if not isinstance(ph, dict):
        ph = {}
        data["protocol_handler"] = ph
    excl = ph.setdefault("excluded_schemes", {})
    if not isinstance(excl, dict):
        excl = {}
        ph["excluded_schemes"] = excl

    for s in TEAMS_SCHEMES:
        #  true = 永不询问、永不打开
        excl[s] = True

    #  ★ 原子写：这个 Preferences 文件是**浏览器自己的配置**，
    #    写坏了下一次 Edge 启动可能读不回 profile（登录态就跟着没了）。
    #    compact=True 保持原本的紧凑格式，不把文件撑大。
    #    backup=False：那是浏览器管理的文件，不该在旁边留我们的 .bak。
    try:
        from . import store
        if not store.write_json_atomic(pref_file, data,
                                       backup=False, compact=True):
            return {"ok": False, "why": "写配置失败（见日志）"}
    except OSError as e:
        return {"ok": False, "why": f"写配置失败：{e}"}

    log.info("已拉黑 Teams 协议：%s", ", ".join(TEAMS_SCHEMES))
    return {"ok": True, "schemes": list(TEAMS_SCHEMES)}


def is_hardened(profile_dir: Path) -> bool:
    """检查配置是否已经拉黑过（省得每次启动都重写）。"""
    pref_file = Path(profile_dir) / "Default" / "Preferences"
    if not pref_file.exists():
        return False
    try:
        d = json.loads(pref_file.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, ValueError):
        return False
    excl = (((d or {}).get("protocol_handler") or {})
            .get("excluded_schemes") or {})
    return all(excl.get(s) is True for s in ("ms-teams", "msteams"))


# ══════════════════════════════════════════════════════════
#  防线二：跑起来之后清场
# ══════════════════════════════════════════════════════════

def looks_like_launcher(url: str, title: str = "") -> bool:
    """判断当前是不是「打开桌面版」中转页。"""
    u = (url or "").lower()
    if any(h in u for h in LAUNCHER_HINTS):
        return True
    t = (title or "").strip().lower()
    return bool(t) and any(x in t for x in LAUNCHER_TITLES)


#  ★ 中转页上的「改用 Web 应用」按钮。
#    点它 = 在网页版里打开目标频道，保留了原本要去的上下文。
#    顺带干掉 src 以 msteams: 开头的 iframe —— 那个才是弹系统框的元凶。
JS_USE_WEB = r"""
() => {
  const b = document.getElementById('openTeamsClientInBrowser');
  if (!b) return {ok: false, why: 'no-browser-button'};

  let killed = 0;
  document.querySelectorAll('iframe').forEach(f => {
    const s = (f.src || '').toLowerCase();
    if (s.startsWith('msteams:') || s.indexOf('msteams:') >= 0) {
      try { f.remove(); killed++; } catch (e) {}
    }
  });

  const r = b.getBoundingClientRect();
  const cx = r.x + r.width / 2, cy = r.y + r.height / 2;
  const mk = t => new MouseEvent(t, {
    bubbles: true, cancelable: true, view: window, button: 0, buttons: 1,
    clientX: cx, clientY: cy,
  });
  try { b.focus && b.focus(); } catch (e) {}
  for (const t of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
    try { b.dispatchEvent(mk(t)); } catch (e) {}
  }
  return {ok: true, killedFrames: killed,
          text: (b.innerText || '').trim().slice(0, 30)};
}
"""


#  清场脚本：点掉各种横幅 / 弹层。
#  刻意用「文字匹配 + 保守点击」，不碰任何可能误伤的东西。
JS_DISMISS = r"""
() => {
  const hit = [];

  //  ── ① 明确的关闭按钮 ──
  const closeSelectors = [
    '[data-tid="banner-notification-dismiss"]',
    '[data-tid*="banner"][data-tid*="dismiss"]',
    '[aria-label="关闭"]', '[aria-label="Close"]',
    '[aria-label="Dismiss"]', '[aria-label="关闭横幅"]',
  ];
  for (const sel of closeSelectors) {
    document.querySelectorAll(sel).forEach(el => {
      const r = el.getBoundingClientRect();
      if (r.width < 4 || r.height < 4) return;
      try {
        el.dispatchEvent(new MouseEvent('click',
          {bubbles: true, cancelable: true, view: window}));
        hit.push('close:' + sel);
      } catch (e) {}
    });
  }

  //  ── ② 「拒绝桌面版 / 以后再说」类的按钮 ──
  //  只在**弹层/横幅**范围内找，避免误点正文里的按钮。
  const NO_WORDS = [
    '取消', '关闭', '不用了', '否', '暂不', '以后再说', '稍后', '不再提示',
    'not now', 'no thanks', 'no, thanks', 'later', 'cancel', 'dismiss',
    'stay in browser', 'continue in browser', '在浏览器中继续', '留在浏览器',
  ];
  const inOverlay = el => {
    let n = el, g = 0;
    while (n && g++ < 10) {
      const a = n.getAttribute && (n.getAttribute('role') || '');
      const tid = (n.getAttribute && n.getAttribute('data-tid')) || '';
      const cls = (typeof n.className === 'string') ? n.className : '';
      if (a === 'dialog' || a === 'alertdialog' || a === 'alert') return true;
      if (/dialog|modal|banner|popup|callout|overlay|toast/i.test(cls)) return true;
      if (/dialog|modal|banner|popup|callout|notification/i.test(tid)) return true;
      n = n.parentElement;
    }
    return false;
  };

  document.querySelectorAll('button,[role="button"],a').forEach(el => {
    if (el.closest('#setup')) return;                 // 别碰我们自己的界面
    const t = (el.innerText || el.getAttribute('aria-label') || '')
      .replace(/\s+/g, ' ').trim().toLowerCase();
    if (!t || t.length > 26) return;
    if (!NO_WORDS.some(w => t === w || t.startsWith(w))) return;
    if (!inOverlay(el)) return;
    const r = el.getBoundingClientRect();
    if (r.width < 6 || r.height < 6) return;
    const cx = r.x + r.width / 2, cy = r.y + r.height / 2;
    const mk = ty => new MouseEvent(ty, {
      bubbles: true, cancelable: true, view: window, button: 0, buttons: 1,
      clientX: cx, clientY: cy,
    });
    try {
      for (const ty of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
        el.dispatchEvent(mk(ty));
      }
      hit.push('no:' + t.slice(0, 20));
    } catch (e) {}
  });

  //  ── ③ 「下载桌面版」侧边条 / 卡片的关闭叉 ──
  document.querySelectorAll('[data-tid*="desktop"],[data-tid*="Desktop"]').forEach(el => {
    const r = el.getBoundingClientRect();
    if (r.width < 20 || r.height < 20) return;
    if (r.width > 900) return;                        // 太大肯定是容器，不是卡片
    const btn = el.querySelector('button[aria-label],[role="button"]');
    if (btn) {
      try {
        btn.dispatchEvent(new MouseEvent('click',
          {bubbles: true, cancelable: true, view: window}));
        hit.push('desktop-card');
      } catch (e) {}
    }
  });

  return hit;
}
"""


def dismiss_popups(page, tries: int = 1) -> list[str]:
    """在页面里点掉能点掉的横幅 / 弹层。返回动作记录（供日志）。"""
    out: list[str] = []
    for _ in range(tries):
        try:
            r = page.eval(JS_DISMISS)
        except Exception:
            break
        if isinstance(r, list) and r:
            out.extend(r)
        else:
            break
    return out


def recover_from_launcher(page, home_url: str = "https://teams.cloud.microsoft/",
                          quiet: bool = False) -> bool:
    """如果当前是「打开桌面版」中转页，退回网页版。

    ★★ 中转页长这样（实测）：
        https://teams.cloud.microsoft/dl/launcher/launcher.html?url=%2F_%23%2Fl%2Fchannel%2F...
        标题「加入对话」，上面有：
            #downloadDesktopClient       「下载 Windows 应用」（别点）
            #openTeamsClientInBrowser    「改用 Web 应用」    ← 点这个
            #tryProtocolLaunchTeamsCl    「立即启用」（= 唤起桌面版，别点）
        外加一个 0x0 的 iframe，src 是 ``msteams:/l/channel/...``
        —— 这个 iframe 是「是否打开 Teams 桌面版」系统弹窗的元凶。

    ★ 为什么点按钮比直接导航好：
        直接 goto 首页会把「本来想去的那个频道」丢掉；
        点「改用 Web 应用」则保留频道上下文，直接落到目标频道。
        实测：点完之后左侧「常规」被正确选中。
    """
    try:
        url = page.eval("location.href") or ""
        title = page.eval("document.title") or ""
    except Exception:
        return False
    if not looks_like_launcher(str(url), str(title)):
        return False

    if not quiet:
        log.info("检测到「打开桌面版」中转页，改走网页版")

    #  ① 首选：点「改用 Web 应用」（保留频道上下文）
    try:
        r = page.eval(JS_USE_WEB)
        if isinstance(r, dict) and r.get("ok"):
            if not quiet:
                log.info("已点「改用 Web 应用」（顺手清掉 %s 个 msteams iframe）",
                         r.get("killedFrames", 0))
            #  等主界面起来
            for _ in range(20):
                time.sleep(1.2)
                try:
                    n = page.eval(
                        "document.querySelectorAll("
                        "'[data-tid=\"channel-list-item\"],"
                        "[data-tid$=\"-team-card\"],"
                        "[data-tid=\"message-pane-body\"]').length") or 0
                except Exception:
                    n = 0
                if n > 0:
                    return True
            return True                       # 点成功了，只是还没渲染完
    except Exception as e:
        log.debug("点「改用 Web 应用」失败：%s", e)

    #  ② 兜底：回首页
    try:
        page.goto(home_url, timeout=40000)
    except Exception as e:
        log.debug("退回网页版失败：%s", e)
        return False
    time.sleep(2.5)
    try:
        dismiss_popups(page)
    except Exception:
        pass
    return True


def guard(page, home_url: str = "https://teams.cloud.microsoft/",
          quiet: bool = False) -> dict:
    """一次完整清场：先看有没有中转页，再点横幅。

    读数据前后各调一次即可，开销很小（一次 eval）。
    """
    rec = {"recovered": False, "dismissed": []}
    rec["recovered"] = recover_from_launcher(page, home_url, quiet=quiet)
    rec["dismissed"] = dismiss_popups(page)
    return rec
