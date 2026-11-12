"""按用户描述的流程实际走一遍：找 101 Middle School → English Corner → PDF。

【为什么单独写】
  2026-09-25 实测发现 Teams 已改版：
    · 域名从 teams.microsoft.com 变成 **teams.cloud.microsoft**
    · 旧选择器 `simple-collab-rail` / `app-layout-area--rail` **已不存在**
    · 左侧导航按钮的 data-tid 是**随机 UUID**（每次加载都变），
      只能按**文字**（活动/聊天/团队/…）来找
    · 容器 tid 变成 `app-layout-area--nav` / `--mid-nav` / `--sub-nav`
  这对「团队/频道」的读取影响很大，所以要实地量一遍。

【这个脚本做什么】
  ① 点左侧「团队」
  ② 读团队列表（找含 101 / Middle 的）
  ③ 点进目标团队
  ④ 读频道列表（找 English Corner / EC）
  ⑤ 进频道，读消息，**把正文与附件分开列出来**
     —— 用来验证「名单在 PDF 附件里，正文只有通知」

只读 + 点击导航，不改动任何远端内容。

用法：python _tools/probe_ec_flow2.py
产物：_recon/ec_flow2.txt
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import cdp  # noqa: E402

PROFILE = Path.home() / "AppData/Local/ManageBac-packer/data/browser-profile"
OUT = ROOT / "_recon"
OUT.mkdir(exist_ok=True)
LOG = OUT / "ec_flow2.txt"

_lines: list[str] = []


def say(msg: str = "") -> None:
    print(msg)
    _lines.append(msg)


def flush() -> None:
    try:
        LOG.write_text("\n".join(_lines), encoding="utf-8")
    except Exception:
        pass


# ── 点左侧导航（按文字，不按 tid —— tid 是随机 UUID）──
JS_CLICK_NAV = r"""
(label) => {
  const want = String(label || '');
  const btn = [...document.querySelectorAll('button, [role="button"], [role="tab"]')]
    .find(e => (e.innerText || '').trim() === want
            || (e.getAttribute('aria-label') || '').trim() === want);
  if(!btn) return {ok: false, why: 'not-found'};
  btn.click();
  return {ok: true, clicked: want};
}
"""

# ── 读团队列表（队卡在 sub-nav / mid-nav 里）──
JS_TEAMS = r"""
() => {
  const out = {ok: false, teams: [], where: ''};
  const zones = [
    '[data-tid="app-layout-area--sub-nav"]',
    '[data-tid="app-layout-area--mid-nav"]',
    '[data-tid="app-layout-area--nav"]',
  ];
  let scope = null;
  for(const z of zones){ const e = document.querySelector(z); if(e){ scope = e; out.where = z; break; } }
  if(!scope) scope = document.body;

  const seen = new Set();
  scope.querySelectorAll('[role="treeitem"], [role="option"], li, button, a').forEach(e => {
    const t = (e.innerText || '').trim().split('\n')[0];
    if(!t || t.length > 70) return;
    if(/^(活动|聊天|团队|分配|日历|通话|应用商店|所有团队|发布|更多)$/.test(t)) return;
    if(seen.has(t)) return;
    seen.add(t);
    out.teams.push(t);
  });
  out.teams = out.teams.slice(0, 60);
  out.ok = out.teams.length > 0;
  return out;
}
"""

# ── 点一个列表项（按文字包含）──
JS_CLICK_ITEM = r"""
(name) => {
  const want = String(name || '').toLowerCase();
  const zones = ['[data-tid="app-layout-area--sub-nav"]',
                 '[data-tid="app-layout-area--mid-nav"]',
                 '[data-tid="app-layout-area--nav"]'];
  let scope = null;
  for(const z of zones){ const e = document.querySelector(z); if(e){ scope = e; break; } }
  if(!scope) scope = document.body;
  const items = [...scope.querySelectorAll(
    '[role="treeitem"], [role="option"], [data-tid="channel-list-item"], li, button, a')];
  const hit = items.find(e => (e.innerText || '').toLowerCase().includes(want));
  if(!hit) return {ok: false, why: 'not-found'};
  hit.click();
  return {ok: true, clicked: (hit.innerText || '').trim().split('\n')[0]};
}
"""

# ── 读当前频道消息：正文 / 附件分开 ──
JS_READ = r"""
() => {
  const out = {ok: false, posts: [], why: '', url: location.href};
  const pane = document.querySelector('[data-tid="message-pane-body"]')
            || document.body;
  const blocks = [...pane.querySelectorAll('[data-tid="channel-pane-message"]')];
  if(!blocks.length) out.why = 'no-message-blocks';

  const RE_FILE = /\.[A-Za-z0-9]{2,5}$/;
  blocks.slice(-30).forEach(b => {
    /*  附件：看 file-attachment-grid / aria-label / title */
    const atts = new Set();
    b.querySelectorAll('[data-tid*="file"], [data-tid*="attach"], [aria-label], [title]')
      .forEach(c => {
        const raw = (c.getAttribute('aria-label') || c.getAttribute('title') || '');
        raw.split('\n').forEach(line => {
          const s = line.trim();
          if(RE_FILE.test(s) && s.length < 200) atts.add(s);
        });
      });
    /*  正文：去掉附件名与「N 个xx表情」这些噪音 */
    const txt = (b.innerText || '')
      .split('\n').map(s => s.trim())
      .filter(s => s
        && !RE_FILE.test(s)
        && !/个.{0,6}表情符号/.test(s)
        && !/打开来自|已编辑|更(多|少)$/.test(s))
      .join(' / ');
    out.posts.push({
      text: txt.slice(0, 260),
      attachments: [...atts],
      hasPdf: [...atts].some(a => a.toLowerCase().endsWith('.pdf')),
    });
  });
  out.ok = out.posts.length > 0;
  return out;
}
"""


def main() -> int:
    say("=" * 72)
    say("  EC 链路实测（Teams 新版）")
    say("=" * 72)

    port = cdp.read_devtools_port(PROFILE)
    if not port:
        say("[X] 组件 Edge 没在跑 —— 先在组件里点「打开 Teams」")
        flush()
        return 2

    c = cdp.CDP(port)
    try:
        ts = [x for x in c.targets() if x.get("type") == "page"]
        if not ts:
            say("[X] 没有标签页")
            flush()
            return 2
        t = ts[0]
        pg = cdp.Page(c, c.attach(t["targetId"]), t["targetId"])
        say(f"  页面: {(t.get('url') or '')[:100]}")
        say(f"  标题: {(t.get('title') or '')[:100]}")

        # ① 点「团队」
        say("\n── ① 点左侧「团队」──")
        for lbl in ("团队", "Teams"):
            r = pg.call(JS_CLICK_NAV, lbl)
            say(f"  点「{lbl}」: {r}")
            if r.get("ok"):
                time.sleep(4)
                break

        # ② 团队列表
        say("\n── ② 团队列表 ──")
        r = pg.eval(JS_TEAMS)
        say(f"  作用域: {r.get('where') or '(body)'}  共 {len(r.get('teams') or [])} 项")
        teams = r.get("teams") or []
        for x in teams:
            lo = x.lower()
            marks = []
            if "101" in lo:
                marks.append("含101")
            if "middle" in lo:
                marks.append("含middle")
            if "high" in lo:
                marks.append("含high")
            say(f"    · {x}" + (f"   ← {'/'.join(marks)}" if marks else ""))

        # ③ 找目标
        say("\n── ③ 找「101 Middle School」──")
        target = ""
        for x in teams:
            lo = x.lower()
            if "101" in lo and "middle" in lo:
                target = x
                break
        if not target:
            for x in teams:
                if "101" in x.lower():
                    target = x
                    break
        if target:
            say(f"  选中: {target}")
            r = pg.call(JS_CLICK_ITEM, target)
            say(f"  点进结果: {r}")
            time.sleep(4)
        else:
            say("  [!] 团队列表里没有含 101 的 —— 列表可能还没展开")

        # ④ 频道
        say("\n── ④ 频道列表 ──")
        r = pg.eval(r"""() => {
          const out = {items: []};
          /* 频道项有稳定的 data-tid="channel-list-item" */
          document.querySelectorAll('[data-tid="channel-list-item"]').forEach(e => {
            const t = (e.innerText || '').trim().split('\n')[0];
            if(t) out.items.push(t);
          });
          if(!out.items.length){
            const zone = document.querySelector('[data-tid="app-layout-area--mid-nav"]');
            if(zone) [...zone.querySelectorAll('li, [role="treeitem"]')].forEach(e => {
              const t = (e.innerText || '').trim().split('\n')[0];
              if(t && t.length < 70) out.items.push(t);
            });
          }
          out.items = [...new Set(out.items)];
          return out;
        }""")
        chans = r.get("items") or []
        say(f"  共 {len(chans)} 个频道")
        for x in chans:
            lo = x.lower()
            m = "   ← EC" if ("english" in lo or "corner" in lo or "roster" in lo) else ""
            say(f"    · {x}{m}")

        # ⑤ 进 EC
        ec = ""
        for x in chans:
            lo = x.lower()
            if "english" in lo or "corner" in lo or "roster" in lo:
                ec = x
                break
        say(f"\n── ⑤ 进入 EC 频道：{ec or '(没找到)'} ──")
        if ec:
            r = pg.call(JS_CLICK_ITEM, ec)
            say(f"  点进结果: {r}")
            time.sleep(6)

        # ⑥ 正文 vs 附件
        say("\n── ⑥ 正文 / 附件 对照 ──")
        r = pg.eval(JS_READ)
        posts = r.get("posts") or []
        say(f"  读到 {len(posts)} 条消息（why={r.get('why') or '-'}）")
        n_att = n_pdf = 0
        for i, p in enumerate(posts[-14:], 1):
            atts = p.get("attachments") or []
            if atts:
                n_att += 1
            if p.get("hasPdf"):
                n_pdf += 1
            say(f"\n  [{i}] 正文: {(p.get('text') or '')[:140]}")
            for a in atts:
                say(f"        附件: {a[:110]}"
                    + ("   ← PDF" if a.lower().endswith(".pdf") else ""))
            if not atts:
                say("        附件: （无）")
        say(f"\n  → 带附件的消息 {n_att} 条，其中含 PDF 的 {n_pdf} 条")

    finally:
        try:
            c.close()
        except Exception:
            pass

    say("\n" + "=" * 72)
    say(f"  报告：{LOG}")
    say("=" * 72)
    flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
