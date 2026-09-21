"""Teams 页面读取 —— 用 CDP 在真实 Edge 里抓消息。

【思路（参考 CampusDesk 的浏览器会话模式）】
  1. 启动组件自带的 Edge（独立 profile，不影响日常浏览器）
  2. 打开 teams.microsoft.com
  3. 用户在里面正常登录（一次，之后会话保持）
  4. 程序注入一段 JS，读页面里**已渲染**的消息
  5. 把结果传回 Python，做分类与存储

【为什么这样做，而不是调 Graph API】
  · Graph 需要注册 Azure 应用 + 学校管理员批准 —— 普通学生做不到
  · 浏览器会话模式用的是「用户自己已经登录的权限」，
    读到的东西 = 他在网页上能看到的东西，不多不少
  · 代价：Teams 是虚拟滚动，只能读到已渲染部分；
    页面改版会失效。这两点都在界面上如实说明。

【只看不点】
  注入的 JS 只做 querySelector / innerText 读取，
  不点击、不提交、不发送任何东西。

★ 选择器说明
  下面用的 data-tid 属性来自 Teams 的 Web 客户端。
  它们**没有对本校账号验证过**（我没有你的账号），
  所以：
    · 识别不到时返回 ok=False，界面上给「手动粘贴」的退路
    · 不做「猜一个最像的元素」这种操作
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

from . import cdp, config, teams

log = logging.getLogger("teams.reader")

TEAMS_URL = "https://teams.microsoft.com/"

# ══════════════════════════════════════════════════════════════════
#  注入到页面的读取脚本
#
#  ★ 只读，不改动页面。返回 JSON 字符串。
# ══════════════════════════════════════════════════════════════════

READ_JS = r"""
(function(){
  try{
    const OUT = {ok:false, reason:'', url:location.href, title:document.title,
                 channel:'', messages:[]};

    /* ① 先确认真的在 Teams 上 */
    const host = location.hostname.toLowerCase();
    if(host.indexOf('teams.microsoft.com') < 0 &&
       host.indexOf('teams.cloud.microsoft') < 0){
      OUT.reason = '当前页面不是 Teams（' + host + '）';
      return JSON.stringify(OUT);
    }

    /* ② 判断有没有登录（有密码框 = 没登录） */
    const pw = document.querySelectorAll('input[type=password]');
    let hasPw = false;
    pw.forEach(function(e){
      if(e.getClientRects && e.getClientRects().length) hasPw = true;
    });
    if(hasPw){
      OUT.reason = '需要先登录 Teams';
      return JSON.stringify(OUT);
    }

    /* ③ 当前在哪个频道 */
    const chNode = document.querySelector(
      '[data-tid="channel-name"], [data-tid="channel-header-title"], ' +
      '[data-tid="chat-header-title"], [data-tid="team-name"]');
    OUT.channel = chNode ? (chNode.innerText || '').trim().slice(0, 120) : '';

    /* ④ 找消息卡片
       多个候选选择器 —— Teams 不同版本用的 data-tid 不完全一样 */
    const CARD_SEL = [
      '[data-tid="chat-pane-message"]',
      '[data-tid="channel-post"]',
      '[data-tid="channel-message"]',
      '[data-tid="assignment-card"]',
      '[data-tid="message-pane-list-item"]',
      '[role="listitem"][data-tid]'
    ].join(',');

    const BODY_SEL = [
      '[data-tid="message-body"]',
      '[data-tid="messageBodyContent"]',
      '[data-tid="chat-pane-message-body"]',
      '[data-tid="channel-post-content"]',
      '[data-tid="post-body"]',
      '.message-body-content'
    ].join(',');

    const TITLE_SEL = [
      '[data-tid="assignment-title"]',
      '[data-tid="message-subject"]',
      '[data-tid="post-subject"]'
    ].join(',');

    const TIME_SEL = [
      '[data-tid="message-timestamp"]',
      '[data-tid="timestamp"]',
      'time[datetime]'
    ].join(',');

    const AUTHOR_SEL = [
      '[data-tid="message-author-name"]',
      '[data-tid="message-author"]',
      '[data-tid="post-author"]'
    ].join(',');

    let cards = Array.prototype.slice.call(document.querySelectorAll(CARD_SEL));

    /* 过滤不可见的（折叠起来的、被隐藏的） */
    cards = cards.filter(function(c){
      if(c.closest('[hidden], [aria-hidden="true"]')) return false;
      if(c.getClientRects && !c.getClientRects().length) return false;
      const st = getComputedStyle(c);
      return st.display !== 'none' && st.visibility !== 'hidden';
    });

    /* 嵌套的卡片归到最外层，避免同一条消息读多次 */
    const outer = cards.filter(function(c){
      return !cards.some(function(o){ return o !== c && o.contains(c); });
    });

    /* ⑤ 逐条读 */
    outer.slice(-100).forEach(function(card){
      const bodyNode = card.querySelector(BODY_SEL);
      const titleNode = card.querySelector(TITLE_SEL);
      const timeNode = card.querySelector(TIME_SEL);
      const authorNode = card.querySelector(AUTHOR_SEL);

      const rd = function(n){
        if(!n) return '';
        let t = (n.innerText != null) ? n.innerText : (n.textContent || '');
        return String(t).replace(/\r\n?/g, '\n').trim();
      };

      const body = rd(bodyNode);
      const title = rd(titleNode).split('\n')[0].trim();
      if(!body && !title) return;

      if(body.length > 16000) return;      /* 超长跳过 */

      /* 附件：只收安全的域名 */
      const atts = [];
      let attCount = 0;
      const links = card.querySelectorAll('a[href]');
      links.forEach(function(a){
        const href = a.getAttribute('href') || '';
        const inAtt = a.closest(
          '[data-tid="attachment"], [data-tid="file-attachment"], ' +
          '[data-tid="attachment-card"], [data-tid="file-card"]');
        if(!inAtt && !/\.(pdf|docx?|xlsx?|pptx?|txt|csv)$/i.test(href)) return;
        attCount++;
        const name = (rd(a) || a.getAttribute('aria-label') || '附件').trim();
        if(/^https:/i.test(href)){
          atts.push({name: name.slice(0, 200), url: href});
        }
      });

      /* 时间：只要带时区的完整格式 */
      let iso = '';
      if(timeNode){
        const dt = timeNode.getAttribute('datetime')
                || timeNode.getAttribute('data-due-date') || '';
        if(dt) iso = dt;
      }

      OUT.messages.push({
        title: title,
        text: body,
        author: rd(authorNode).split('\n')[0].trim().slice(0, 120),
        channel: OUT.channel,
        time: iso,
        timeLabel: rd(timeNode).split('\n')[0].trim().slice(0, 80),
        attachments: atts,
        attachCount: attCount,
      });
    });

    if(OUT.messages.length === 0){
      OUT.reason = '页面上没有识别到消息。可能还在加载，或需要打开具体频道。';
      return JSON.stringify(OUT);
    }

    OUT.ok = true;
    return JSON.stringify(OUT);

  }catch(e){
    return JSON.stringify({ok:false, reason:'读取脚本出错：' + String(e),
                           messages:[]});
  }
})()
"""


#  自动往回滚动，让 Teams 把自己更早的消息加载出来。
#
#  为什么这么做：Teams 是懒加载的，滚动到顶才会继续取历史。
#  参考项目（teams-web-chat-exporter / CampusDesk）走的是「调内部
#  Chat Service API + 分页」——能拿到完整历史，但那个接口没有公开
#  文档、随时会变。这里改用滚动触发 Teams 自己的加载逻辑：
#  效果一样是「把更早的消息取出来」，但不碰任何内部接口。
#
#  ★ 关键：每滚一轮要「等它真的加载完」再滚下一轮。
#    同步连滚多次是没用的 —— Teams 还没取回数据就被下一次滚动打断了。
#    所以这里写成 async：滚动 → 等 scrollHeight 变化 → 再滚。
SCROLL_JS = r"""
(async function(opts){
  var maxRounds = (opts && opts.rounds) || 8;
  var budgetMs  = (opts && opts.budgetMs) || 90000;
  var quietMs   = (opts && opts.quietMs) || 1200;   /* 这么久没变化 = 到头了 */

  var T0 = Date.now();
  var done = function(o){
    o.ms = Date.now() - T0;
    return JSON.stringify(o);
  };
  var sleep = function(ms){ return new Promise(function(r){ setTimeout(r, ms); }); };

  try{
    /* 找消息列表的滚动容器 */
    function findScroller(){
      var cards = document.querySelectorAll(
        '[data-tid="chat-pane-message"], [data-tid="channel-post"], ' +
        '[data-tid="message-pane-list-item"], [data-tid="channel-message"]');
      if(!cards.length) return null;
      var node = cards[cards.length - 1];
      while(node && node !== document.body){
        var st = getComputedStyle(node);
        var oy = st.overflowY;
        if((oy === 'auto' || oy === 'scroll') && node.scrollHeight > node.clientHeight + 40){
          return node;
        }
        node = node.parentElement;
      }
      return null;
    }

    var sc = findScroller();
    if(!sc){
      return done({ok:false, reason:'没找到消息列表的滚动区域',
                   atTop:false, rounds:0, grew:0});
    }

    /*  数一下当前有多少条消息。用它判断「有没有加载出新的」，
        比只看 scrollHeight 可靠 —— 有时高度没变但内容变多了。 */
    function msgCount(){
      return document.querySelectorAll(
        '[data-tid="chat-pane-message"], [data-tid="channel-post"], ' +
        '[data-tid="message-pane-list-item"], [data-tid="channel-message"]').length;
    }

    var n0 = msgCount();
    var h0 = sc.scrollHeight;
    var grew = 0;
    var rounds = 0;
    var atTop = false;

    /*  单轮：往回滚一屏，然后等页面把新内容插进来。
        判据是 scrollHeight 或消息条数变大；最多等 waitMs。 */
    async function oneRound(waitMs){
      var hBefore = sc.scrollHeight;
      var nBefore = msgCount();
      var step = Math.max(400, Math.round(sc.clientHeight * 1.1));
      sc.scrollTop = Math.max(0, sc.scrollTop - step);

      var t = Date.now();
      while(Date.now() - t < waitMs){
        await sleep(150);
        if(sc.scrollHeight !== hBefore || msgCount() !== nBefore){
          /* 还在长就再给它一点时间 */
          await sleep(250);
          return true;
        }
        /* 已经到顶且没变化 → 不用等了，交给外层判停 */
        if(sc.scrollTop <= 2 && sc.scrollHeight === hBefore) break;
      }
      return (sc.scrollHeight !== hBefore || msgCount() !== nBefore);
    }

    for(var i = 0; i < maxRounds; i++){
      if(Date.now() - T0 > budgetMs) break;      /* 总时间到 */
      var changed = await oneRound(2500);
      rounds++;
      if(changed){
        grew++;
        atTop = sc.scrollTop <= 2;
        continue;
      }
      /*  这一轮没加载出新东西。可能只是慢，再给一次机会；
          两次都没动静就认为到头了。 */
      var h2 = sc.scrollHeight;
      await sleep(quietMs);
      if(sc.scrollHeight === h2 && msgCount() === n0 + grew * 0){
        atTop = atTop || sc.scrollTop <= 2;
        break;
      }
    }

    return done({
      ok: true,
      rounds: rounds,
      grew: grew,                       /* 有几轮真的加载出了新内容 */
      messages_before: n0,
      messages_after: msgCount(),
      scrollTop: sc.scrollTop,
      atTop: sc.scrollTop <= 2,
      timedOut: (Date.now() - T0) > budgetMs,
    });
  }catch(e){
    return done({ok:false, reason:String(e), atTop:false, rounds:0, grew:0});
  }
})
"""


def _msg_key(it: dict) -> str:
    """一条消息的指纹：日期 + 标题 + 正文前 80 字。"""
    return (f"{it.get('date')}|{it.get('title')}|"
            f"{(it.get('text') or '')[:80]}")


def _att_key(it: dict) -> str:
    """一个附件的指纹：URL 优先，没有就退回名字。"""
    return (it.get("url") or "") or (it.get("name") or "")


def _merge_keep(new: list, old: list, limit: int, key=_msg_key) -> list:
    """把新旧两批合起来去重，按时间倒序保留最新 limit 条。

    为什么要按时间排序再截断：翻历史时新读到的往往是更早的消息，
    如果只是「新的排前面」然后砍尾巴，刚翻出来的历史反而先被丢掉。
    """
    seen: set[str] = set()
    out: list = []
    for it in list(new) + list(old):
        if not isinstance(it, dict):
            continue
        k = key(it)
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(it)

    #  date 是 YYYY-MM-DD，字符串排序即时间排序。
    #  没有日期的排最后（它们多半是解析不出时间的老记录）——
    #  注意 reverse=True 会把 True 排前面，所以这里用「有日期」倒序。
    def sort_key(it: dict):
        d = (it.get("date") or "")[:10]
        return (1 if d else 0, d)

    out.sort(key=sort_key, reverse=True)
    return out[:limit]


def _scroll_up(rounds: int = 8, budget_sec: int = 90) -> dict:
    """在 Teams 页面里往回滚，把更早的消息带出来。

    rounds     最多滚几轮（每轮约一屏）。轮数不是硬上限 ——
               一旦连续两轮加载不出新内容就提前停。
    budget_sec 总时间上限，避免界面一直卡着。

    返回 {ok, atTop, rounds, grew, messages_before/after, ...}。
    读不到滚动区时返回 ok=False，调用方按「只读当前屏」继续，不算失败。
    """
    from . import browser

    try:
        sess = browser.open_login_window(TEAMS_URL)
    except Exception as e:
        return {"ok": False, "reason": f"无法启动浏览器：{e}", "atTop": False}

    page = _pick_page(sess)
    if page is None:
        return {"ok": False, "reason": "没有 Teams 标签页", "atTop": False}

    opts = {"rounds": int(rounds), "budgetMs": int(budget_sec * 1000)}

    try:
        #  Playwright 的 evaluate 会自动等待 Promise 解析完，
        #  所以 SCROLL_JS 那个 async 函数可以直接调用。
        #  参数用「函数 + 参数」形式传，别拼字符串（避免注入问题）。
        raw = page.evaluate(SCROLL_JS, opts)         # type: ignore[attr-defined]
    except Exception as e:
        return {"ok": False, "reason": f"滚动失败：{e}", "atTop": False}

    try:
        d = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except Exception:
        return {"ok": False, "reason": "滚动返回格式不对", "atTop": False}

    if not isinstance(d, dict):
        return {"ok": False, "reason": "滚动返回格式不对", "atTop": False}

    return d


def _pick_page(sess) -> "cdp.Page | None":
    """在已打开的所有标签里找一个 Teams 页面。"""
    try:
        tgts = sess._cdp.list_targets()          # type: ignore[attr-defined]
    except Exception:
        return None
    for t in tgts:
        url = (t.get("url") or "").lower()
        if "teams.microsoft.com" in url or "teams.cloud.microsoft" in url:
            try:
                return sess._cdp.attach(t["id"])   # type: ignore[attr-defined]
            except Exception:
                continue
    return None


def open_teams(headless: bool = False) -> bool:
    """打开 Teams 页面（复用已开的；没有就新开一个）。

    headless=False 让用户能看见并登录。
    """
    try:
        from . import browser
        sess = browser.open_login_window(TEAMS_URL)
        return bool(sess)
    except Exception as e:
        log.warning("打开 Teams 失败：%s", e)
        return False


def read_page(wait_sec: int = 6) -> dict:
    """读当前 Teams 页面里的消息。

    返回：
        {ok, reason, channel, messages:[...], url}
    """
    from . import browser

    try:
        sess = browser.open_login_window(TEAMS_URL)
    except Exception as e:
        return {"ok": False, "reason": f"无法启动浏览器：{e}", "messages": []}

    page = _pick_page(sess)
    if page is None:
        # 没有 Teams 标签 → 开一个
        try:
            page = sess.page
            page.goto(TEAMS_URL)             # type: ignore[attr-defined]
        except Exception as e:
            return {"ok": False, "reason": f"打不开 Teams：{e}", "messages": []}

    # 等页面渲染（Teams 是 SPA，DOM 出现需要时间）
    time.sleep(max(2, min(wait_sec, 15)))

    try:
        raw = page.evaluate(READ_JS)         # type: ignore[attr-defined]
    except Exception as e:
        return {"ok": False, "reason": f"读取页面失败：{e}", "messages": []}
    if not raw:
        return {"ok": False, "reason": "页面没有返回数据", "messages": []}

    try:
        d = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return {"ok": False, "reason": "返回的数据格式不对", "messages": []}

    return d


def sync(scroll_rounds: int = 8, budget_sec: int = 90) -> dict:
    """执行一次同步：往回翻 → 读页面 → 分类 → 存本地。

    scroll_rounds 最多往回滚几轮（每轮约一屏）。
      0  = 只读当前屏
      8  = 默认，通常够翻到较早的内容
      一旦连续两轮加载不出新内容就提前停，不会白等。

    返回给界面看的摘要。
    """
    # 先把更早的消息滚出来（失败不影响后续读取）
    scrolled: dict = {"ok": False}
    if scroll_rounds > 0:
        scrolled = _scroll_up(rounds=scroll_rounds, budget_sec=budget_sec)

    page = read_page()

    if not page.get("ok"):
        reason = page.get("reason") or "读取失败"
        log.info("Teams 同步未完成：%s", reason)
        # ★ 不清空已有数据 —— 读到就更新，读不到保持原状
        d = teams.load()
        d["note"] = reason
        teams.save(d)
        return {"ok": False, "reason": reason,
                "ec": 0, "posts": 0, "attachments": 0}

    msgs = page.get("messages") or []
    channel = page.get("channel") or ""

    posts, ec_items, atts = [], [], []
    seen_urls: set[str] = set()

    for m in msgs:
        title = (m.get("title") or "").strip()
        body = (m.get("text") or "").strip()
        ch = (m.get("channel") or channel or "").strip()

        # 附件过滤
        safe_atts = []
        for a in (m.get("attachments") or [])[:6]:
            u = teams.safe_url(a.get("url") or "")
            if not u:
                continue
            if u in seen_urls:
                continue
            seen_urls.add(u)
            safe_atts.append({"name": (a.get("name") or "附件")[:200],
                              "url": u})
            atts.append({"name": (a.get("name") or "附件")[:200],
                         "url": u, "channel": ch,
                         "date": (m.get("time") or "")[:10]})

        base = {
            "title": title[:160] or "Teams 消息",
            "text": body,
            "author": m.get("author") or "",
            "channel": ch,
            "date": (m.get("time") or "")[:10],
            "timeLabel": m.get("timeLabel") or "",
            "url": teams.safe_url(page.get("url") or "", allow_teams=True),
            "attachments": safe_atts,
        }

        if teams.is_ec(title, body, ch):
            ec_items.append(dict(base, kind="ec"))
        else:
            posts.append(dict(base, kind="homework"
                              if teams.is_homework(title, body, ch)
                              else "general"))

    old = teams.load()
    #  合并新旧数据。上限给得宽一些：翻历史时会攒下很多条，
    #  截断太早会把早前的 EC 记录挤掉（那正是想查的东西）。
    merged = {
        "posts": _merge_keep(posts, old.get("posts") or [], 1200),
        "ec": _merge_keep(ec_items, old.get("ec") or [], 1200),
        "attachments": _merge_keep(atts, old.get("attachments") or [], 2000,
                                   key=_att_key),
        "note": "",
    }

    teams.save(merged)

    log.info("Teams 同步完成：EC %d 条 / 其他 %d 条 / 附件 %d 个（累计 EC %d / 其他 %d）",
             len(ec_items), len(posts), len(atts),
             len(merged["ec"]), len(merged["posts"]))

    return {
        "ok": True, "reason": "",
        "channel": channel,
        "ec": len(ec_items), "posts": len(posts),
        "attachments": len(atts),
        "scanned": len(msgs),
        #  滚动情况：让界面能如实告诉你「翻到哪了」
        "scrolled": bool(scrolled.get("ok")),
        "at_top": bool(scrolled.get("atTop")),
        "scroll_rounds": int(scrolled.get("rounds") or 0),
        "scroll_grew": int(scrolled.get("grew") or 0),
        "saved_ec": len(merged.get("ec") or []),
        "saved_posts": len(merged.get("posts") or []),
    }


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO)
    print("只做静态检查（不启动浏览器）")
    print("READ_JS 长度  :", len(READ_JS), "字符")
    print("SCROLL_JS 长度:", len(SCROLL_JS), "字符")
    assert "querySelectorAll" in READ_JS
    assert "return JSON.stringify" in READ_JS
    assert "scrollTop" in SCROLL_JS
    assert "scrollHeight" in SCROLL_JS
    print("OK")
