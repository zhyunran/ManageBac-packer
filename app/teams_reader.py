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

TEAMS_URL = "https://teams.cloud.microsoft/"

#  ★ 域名说明（实测出来的）：
#    `teams.microsoft.com/v2/` 现在会**重定向**到 `teams.cloud.microsoft`。
#    用旧地址也能进，但中途要跳一次，加载慢、也容易在跳转期间抓错页面。
#    所以直接开新域名。两个都认，兼容旧书签。
TEAMS_HOSTS = ("teams.cloud.microsoft", "teams.microsoft.com")

# ══════════════════════════════════════════════════════════════════
#  注入到页面的读取脚本
#
#  ★ 只读，不改动页面。返回 JSON 字符串。
# ══════════════════════════════════════════════════════════════════

READ_JS = r"""
(function(){
  try{
    const OUT = {ok:false, reason:'', url:location.href, title:document.title,
                 channel:'', messages:[], sessions:[], partial:false};

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

    /*  ⑥ 顺手把左侧「会话列表」读出来 ——
        用于「同步全部会话」：一个一个点过去，把每个会话的消息都收下来。
        读不到不算失败（Teams 改版可能换选择器），只是同步范围受限。 */
    try{
      const LIST_SEL = [
        '[data-tid="chat-list-item"]',
        '[data-tid="channel-list-item"]',
        '[data-tid="conversation-list-item"]',
        '[role="treeitem"]',
        '[role="listitem"][data-tid*="list"]'
      ].join(',');
      const rows = Array.prototype.slice.call(document.querySelectorAll(LIST_SEL));
      const seen = {};
      rows.forEach(function(r){
        if(r.closest('[hidden]')) return;
        const rects = r.getClientRects ? r.getClientRects() : [];
        if(!rects.length) return;
        const t = rd(r).split('\n')[0].trim();
        if(!t || t.length > 80) return;
        /*  会话名重复的（同一个会话在列表里可能出现多次）只留一个 */
        if(seen[t]) return;
        seen[t] = 1;
        OUT.sessions.push({name: t, active: !!r.getAttribute('aria-selected')});
      });
    }catch(e2){}

    if(OUT.messages.length === 0){
      OUT.reason = '页面上没有识别到消息。可能还在加载，或需要打开具体会话。';
      /*  消息读不到但会话列表读到了 → 也算部分成功，
          调用方可以据此提示「打开某个会话再同步」 */
      if(OUT.sessions.length){
        OUT.ok = true;
        OUT.partial = true;
        return JSON.stringify(OUT);
      }
      return JSON.stringify(OUT);
    }

    OUT.ok = true;
    return JSON.stringify(OUT);

  }catch(e){
    return JSON.stringify({ok:false, reason:'读取脚本出错：' + String(e),
                           messages:[], sessions:[]});
  }
})()
"""


#  点开会话列表里的第 N 项（「同步全部」时逐个点过去用）。
#  返回点了哪个、会话名是什么，方便调用方判断有没有前进。
#
#  ⚠ 零调用：`sync()` 从不调用它，注入 JS 产出的 `sessions` 字段
#    在 Python 侧也没有任何消费者（全项目检索确认）。
#    也就是说「同步全部会话」这个能力**只做到 JS 层，没有接线**。
#
#    这不是删不删的问题，而是**必须标出来**：
#    否则读代码的人会以为「会话列表同步」已经能用，
#    而实际上它从来没被触发过。要补完（把 sessions 用起来）
#    还是删掉，是一个功能决定，不是清理决定。
CLICK_SESSION_JS = r"""
(function(idx){
  try{
    const LIST_SEL = [
      '[data-tid="chat-list-item"]',
      '[data-tid="channel-list-item"]',
      '[data-tid="conversation-list-item"]',
      '[role="treeitem"]',
      '[role="listitem"][data-tid*="list"]'
    ].join(',');
    let rows = Array.prototype.slice.call(document.querySelectorAll(LIST_SEL));
    rows = rows.filter(function(r){
      if(r.closest('[hidden]')) return false;
      const rects = r.getClientRects ? r.getClientRects() : [];
      return rects.length > 0;
    });
    /*  去重：同一个会话名只算一次，否则点来点去都在原地 */
    const seen = {};
    const uniq = [];
    rows.forEach(function(r){
      const t = (r.innerText || r.textContent || '').trim().split('\n')[0];
      if(!t || t.length > 80) return;
      if(seen[t]) return;
      seen[t] = 1;
      uniq.push(r);
    });

    if(idx < 0 || idx >= uniq.length){
      return JSON.stringify({ok:false, reason:'会话下标越界',
                             total: uniq.length});
    }
    const el = uniq[idx];
    const name = (el.innerText || el.textContent || '').trim().split('\n')[0];
    /*  真的去点它 —— Teams 是 React 应用，派发真实鼠标事件才有效 */
    el.scrollIntoView({block:'nearest'});
    ['mousedown','mouseup','click'].forEach(function(t){
      el.dispatchEvent(new MouseEvent(t, {bubbles:true, cancelable:true,
                                          view:window}));
    });
    return JSON.stringify({ok:true, name:name, total: uniq.length});
  }catch(e){
    return JSON.stringify({ok:false, reason:String(e), total:0});
  }
})
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
    page = _get_page()
    if page is None:
        return {"ok": False, "reason": "连不上 Teams 页面", "atTop": False}

    opts = json.dumps({"rounds": int(rounds),
                       "budgetMs": int(budget_sec * 1000)})

    try:
        #  SCROLL_JS 是个 async 函数 —— CDP 的 awaitPromise 会等它跑完。
        raw = page.eval(f"({SCROLL_JS})({opts})", await_promise=True)
    except Exception as e:
        return {"ok": False, "reason": f"滚动失败：{e}", "atTop": False}

    try:
        d = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except Exception:
        return {"ok": False, "reason": "滚动返回格式不对", "atTop": False}

    if not isinstance(d, dict):
        return {"ok": False, "reason": "滚动返回格式不对", "atTop": False}

    return d


# ══════════════════════════════════════════════════════════════════
#  浏览器连接
#
#  ★ 为什么不用 Playwright：
#    Playwright 启动 Edge 时会自动加约 40 个开关，其中两个足以
#    让 Teams 卡死：
#      --disable-sync                 禁用数据同步
#      --disable-background-networking 禁用后台网络
#    Teams 是重度依赖后台同步的应用 —— 被禁掉之后会一直停在
#    「我们正在为你设置内容...」，永远加载不出消息列表。
#
#    所以改成：直接用 app/edge.py 起一个**原生 Edge**
#    （零自动化开关，和用户自己双击打开的完全一样），
#    再通过 CDP 连上去读页面。
#
#  ★ 关于「不干扰用户自己的 Edge」：
#    用的是独立 profile（data/browser-profile），
#    Edge 原生支持多个实例用不同 user-data-dir 并存 ——
#    你照常开自己的 Edge，两边互不影响。
#
#  ★ 关于登录：
#    首次需要你在弹出的窗口里登录一次；登录态由 Edge 自己
#    持久化在这个 profile 里，之后（包括开机预热）都不再需要。
# ══════════════════════════════════════════════════════════════════

#  进程内缓存 CDP 连接，避免每次读都重连（重连要几秒）
_CDP_CACHE: dict = {"cdp": None, "port": 0, "at": 0.0}
_CDP_TTL = 120.0        # 连接缓存 2 分钟


def _connect(force_new: bool = False):
    """连到我们自己的 Edge，返回 (CDP 对象, 端口)。

    没在跑就启动一个（原生 Edge，不是 Playwright）。
    """
    from . import cdp as _cdp
    from . import config, edge, teams_guard

    prof = config.BROWSER_PROFILE_DIR

    #  ① 复用缓存
    now = time.time()
    if (not force_new and _CDP_CACHE["cdp"]
            and now - _CDP_CACHE["at"] < _CDP_TTL):
        return _CDP_CACHE["cdp"], _CDP_CACHE["port"]

    running = bool(edge.read_devtools_port(prof)) and edge.is_edge_running(prof)

    #  ② 已经在跑 → 直接连
    if running:
        port = edge.read_devtools_port(prof)
        try:
            c = _cdp.CDP(port)
            _CDP_CACHE.update(cdp=c, port=port, at=now)
            return c, port
        except Exception as e:
            log.debug("连已有 Edge 失败（%s），尝试重启", e)

    #  ③ 没在跑 → 启动前先拉黑 Teams 协议。
    #     必须在启动**之前**写：该配置只在浏览器启动时读一次，
    #     而且 Edge 运行中退出时会把内存里的旧值写回去。
    if not running:
        try:
            r = teams_guard.harden_profile(prof)
            if not r.get("ok"):
                log.debug("写协议黑名单没成功：%s", r.get("why"))
        except Exception as e:
            log.debug("协议黑名单异常：%s", e)

    #  ④ 启动原生 Edge
    edge.launch_edge(prof, headless=False, url=TEAMS_URL, debug_port=True)
    port = edge.wait_for_devtools(prof, timeout=60)
    c = _cdp.CDP(port)
    _CDP_CACHE.update(cdp=c, port=port, at=now)
    return c, port


def _pick_page(cdp_obj=None):
    """在已打开的标签里找一个 Teams 页面，返回 Page 句柄。

    注意 CDP.attach() 返回的是 **session_id**（字符串），
    要自己包成 Page 才能调 eval。这一步以前漏了 ——
    表现为「拿到了对象但没有 eval 方法」。
    """
    from .cdp import Page as _Page
    try:
        c = cdp_obj or _connect()[0]
        tgts = c.targets()
    except Exception as e:
        log.debug("列出标签失败：%s", e)
        return None
    for t in tgts:
        #  只认真正的页面 —— service_worker / worker 也有 teams 域名，
        #  之前误选过它，导致 eval 报「Uncaught」什么都读不到。
        if t.get("type") != "page":
            continue
        url = (t.get("url") or "").lower()
        if any(h in url for h in TEAMS_HOSTS):
            try:
                sid = c.attach(t["targetId"])
                return _Page(c, sid, t["targetId"])
            except Exception:
                continue
    return None


def _get_page(wait_ready_sec: int = 90):
    """拿到一个可用的 Teams 页面（必要时打开/等待）。"""
    _connect()
    page = _pick_page()
    if page is not None:
        return page

    #  没有 Teams 标签 → 新开一个。
    #  CDP.create() 返回 (target_id, session_id)，
    #  但这里统一走 _pick_page 拿 Page 句柄（保持一致）。
    try:
        c, _port = _connect()
        c.create(TEAMS_URL)
        time.sleep(2.5)             # 等标签建好、URL 就位
        return _pick_page(c)
    except Exception as e:
        log.warning("打开 Teams 标签失败：%s", e)
        return None


def open_teams(headless: bool = False) -> bool:
    """打开 Teams 窗口（复用已开的；没有就新开一个）。

    用的是原生 Edge，和你自己双击打开的一模一样。
    """
    try:
        return _get_page() is not None
    except Exception as e:
        log.warning("打开 Teams 失败：%s", e)
        return False


def wait_teams_ready(timeout: int = 90, quiet: bool = False) -> dict:
    """等 Teams 界面**真的**出来（消息列表可读）。

    Teams 首次加载或换 profile 后要重建索引，可能要 1~2 分钟。
    返回 {ok, waited, reason}。
    """
    from . import teams_guard

    t0 = time.time()
    last_log = 0.0
    guard_at = 0.0
    while time.time() - t0 < timeout:
        page = _pick_page()
        if page is not None:
            #  ★ 每 4 秒清一次场 —— 中转页退回、横幅点掉。
            #    不清的话「等界面就绪」会一直等不到东西，看着就像卡死。
            now = time.time()
            if now - guard_at > 4:
                guard_at = now
                try:
                    g = teams_guard.guard(page, TEAMS_URL, quiet=quiet)
                    if g.get("recovered") and not quiet:
                        log.info("检测到桌面版中转页，已退回网页版")
                except Exception:
                    pass

            try:
                n = page.eval(
                    "document.querySelectorAll("
                    "'[data-tid=\"chat-pane-message\"],"
                    "[data-tid=\"channel-post\"],"
                    "[data-tid=\"channel-pane-message\"],"
                    "[role=\"listitem\"],"
                    "[contenteditable=\"true\"]').length"
                ) or 0
                if isinstance(n, (int, float)) and n > 0:
                    return {"ok": True, "waited": int(time.time() - t0)}
            except Exception:
                pass
        el = time.time() - t0
        if not quiet and el - last_log > 15:
            last_log = el
            log.info("等待 Teams 界面就绪…（%.0f 秒）", el)
        time.sleep(2)
    return {"ok": False, "waited": int(time.time() - t0),
            "reason": "等待超时，Teams 界面还没加载出来"}


def read_page(wait_sec: int = 6) -> dict:
    """读当前 Teams 页面里的消息。

    返回：
        {ok, reason, channel, messages:[...], sessions:[...]}
    """
    page = _get_page()
    if page is None:
        return {"ok": False, "reason": "连不上 Teams 页面", "messages": [],
                "sessions": []}

    #  ★ 清场：万一被「打开桌面版」弹窗挡着，或者被导到中转页，
    #    这里先救回来。不做的话会一直读不到元素 —— 表现为「卡死」。
    try:
        from . import teams_guard
        g = teams_guard.guard(page, TEAMS_URL)
        if g.get("recovered"):
            log.info("已从「打开桌面版」中转页退回网页版")
        if g.get("dismissed"):
            log.debug("点掉横幅：%s", g["dismissed"])
    except Exception as e:
        log.debug("清场跳过：%s", e)

    # 等页面渲染（Teams 是 SPA）
    time.sleep(max(2, min(wait_sec, 15)))

    try:
        raw = page.eval(READ_JS)
    except Exception as e:
        return {"ok": False, "reason": f"读取页面失败：{e}", "messages": [],
                "sessions": []}
    if not raw:
        return {"ok": False, "reason": "页面没有返回数据", "messages": [],
                "sessions": []}

    try:
        d = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return {"ok": False, "reason": "返回的数据格式不对", "messages": [],
                "sessions": []}

    return d


def _read_ec_via_crawl() -> dict | None:
    """兜底：落到缓存里的 EC 频道，用全量爬取那套读法读当前屏。

    ★ 为什么需要它：
      ① 全量收取收尾会把浏览器带回 Teams 首页（_finish 回首页），
        之后点「同步」读到的是首页，一条消息都没有；
      ② 本模块的 READ_JS 是 v0.4 为聊天视图写的，认不得频道页的
        data-tid="channel-pane-message" —— 所以落到频道后要改用
        teams_crawl.JS_READ（全量收取每天在用的那套）来读。

    返回 {ok, messages, channel, sessions} 或 None（落位/读取失败）。
    消息已映射成 sync() 期望的形状。附件不在此处读 ——
    附件由「取名单」流程专门处理。
    """
    try:
        from . import ec_fetch, teams_crawl
        loc = ec_fetch.find_channel()
        cid = (loc.get("channel_id") or "").strip()
        if not cid:
            return None
        page = _get_page(wait_ready_sec=30)
        if page is None:
            return None
        page.goto(teams_crawl.CHANNEL_URL + cid, timeout=45000)
        st = teams_crawl._wait_for_channel(page, tries=14)      # noqa: SLF001
        if st.get("launcher"):
            #  落到「加入对话」中转页 → 点「改用 Web 应用」拉回来
            teams_crawl._guard(page)                            # noqa: SLF001
            st = teams_crawl._wait_for_channel(page, tries=10)  # noqa: SLF001
        if not (st.get("pane") or (st.get("msgs") or 0) > 0 or st.get("empty")):
            return None
        d = page.eval(teams_crawl.JS_READ) or {}
        if not isinstance(d, dict) or not d.get("ok"):
            return None
        ch_name = (loc.get("channel") or d.get("channel") or "").strip()
        msgs = []
        for p in (d.get("posts") or []):
            tm = (p.get("time") or "").strip()
            msgs.append({
                "title": (p.get("title") or "")[:200],
                "text": (p.get("text") or "")[:16000],
                "author": (p.get("author") or "")[:120],
                "channel": ch_name,
                "time": tm[:10],
                "timeLabel": tm[:80],
                "attachments": [],
                "attachCount": 0,
            })
        log.info("已自动落到 EC 频道并读出 %d 帖：%s / %s",
                 len(msgs), loc.get("team", ""), ch_name)
        return {"ok": True, "messages": msgs, "channel": ch_name,
                "sessions": []}
    except Exception as e:
        log.debug("EC 兜底读取失败：%s", e)
        return None


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

    #  Teams 加载波动大（约 1/5 的首次读取会空手），
    #  读不到消息时等几秒再读一次，多半就回来了 —— 别一次就认输。
    if not page.get("ok") and "没有识别到消息" in str(page.get("reason") or ""):
        time.sleep(6)
        page = read_page()

    #  还是没读到 → 多半是全量收取收尾时把浏览器带回了 Teams 首页
    #  （teams_crawl._finish 回首页），首页没打开任何频道，自然没消息。
    #  而且 READ_JS 是 v0.4 为聊天视图写的，认不得频道页的
    #  channel-pane-message —— 落到频道后要改用爬取那套读法。
    if not page.get("ok") and "没有识别到消息" in str(page.get("reason") or ""):
        fb = _read_ec_via_crawl()
        if fb:
            page = fb

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
