"""Teams 全量抓取 —— 用户零操作，把每个团队的每个频道的消息全读回来。

【为什么单独做这个模块】
    原来的 ``teams.py`` 只读「当前打开的那个频道」——
    要换频道就得让用户自己点，这不实用。

    用户的需求是：**打开组件就能看到 Teams 全部内容**，
    不需要自己去 Teams 网页里点来点去。

【怎么做到「不用点」】
    实测发现 Teams 支持频道级深度链接：

        https://teams.cloud.microsoft/l/channel/{channelId}

    直接导航过去，Teams 会自己加载该频道的消息。
    于是全量抓取变成两阶段：

        ① **枚举**：进每个团队（点团队卡片，一次），拿到频道列表 + 频道 ID
        ② **抓取**：对每个频道 ID 拼深度链接 → 导航 → 读 DOM

    阶段 ② 完全不依赖点击，快且稳。

【已实测的 DOM 选择器】（2025-09 验证于 teams.cloud.microsoft）
    [data-tid="message-pane-body"]                    消息面板
      └ [data-tid="channel-pane-viewport"]            滚动容器（虚拟列表）
         └ [data-tid="channel-pane-runway"]           消息跑道
            └ DIV (xN)                                每个是一条帖子
               └ [data-tid="channel-pane-message"]
                  ├ [data-tid="post-message-subheader"]   作者 + 时间 +「已编辑」
                  ├ H1/H2/H3                              帖子标题
                  ├ P                                     正文段落
                  └ [data-tid="response-surface"]
                     └ [data-tid="reply-message-header"]  回复作者 + 时间
    [data-tid="channel-list-item"]                    左侧频道项
    [data-tid^="channel-list-item-text-"]             频道名（data-tid 后缀是频道 ID）
    [data-tid$="-team-card"]                          团队卡片（data-tid 去掉后缀是团队 ID）
    [data-tid="channel-empty-state"]                  空频道提示

【边界】
  · 只读，绝不发消息、不点表情、不提交任何东西
  · 只抓**已渲染**的消息（Teams 是虚拟滚动，更早的需要往上滚才出现）
  · 读不到就保持原数据，不清空（防止页面改版把历史数据冲掉）
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, timedelta
from pathlib import Path

from . import config
from . import store
from . import teams_scroll

log = logging.getLogger(
"teams_crawl")


# ── 取消（「停止」按钮）──────────────────────────────
#  ★ 深度归档最长要跑 90 分钟，以前完全停不下来 ——
#    只能等它自己结束，或者杀掉整个程序。
#  用一个 Event：read_channel 的滚动循环、crawl_all 的频道循环都会看它。
import threading as _threading

_CANCEL = _threading.Event()


def request_cancel() -> None:
    """请求停止当前抓取（下一次检查点生效，秒级）。"""
    _CANCEL.set()


def clear_cancel() -> None:
    """开始新一轮抓取前清掉上次的停止请求。"""
    _CANCEL.clear()


def cancelled() -> bool:
    return _CANCEL.is_set()


HOME_URL = "https://teams.cloud.microsoft/"

# ══════════════════════════════════════════════════════════
#  中文表情名 → 真 emoji
#
#  ★ 为什么 Python 这边也要有一份：
#    JS 那份（在 JS_READ 里）管的是**抓取时**把 <img alt> 换成 emoji。
#    这份管的是**读老缓存时**从噪音里捞反应 ——
#    老数据里没有 reactions 字段，只有一句
#    「3 个赞表情符号。」，靠这张表才能还原成 {emoji:'👍', n:3}。
#
#    两边都出现的名字必须一致（'大笑'→😄 之类），
#    改动时记得同步改两处。
# ══════════════════════════════════════════════════════════
EMO_CN: dict[str, str] = {
    #  ── 常用 ──
    "好的": "👍", "赞": "👍", "同意": "👍", "支持": "👍", "顶": "👍",
    "大拇指": "👍", "不行": "👎", "向下": "👎",
    "心": "❤️", "红心": "❤️", "爱心": "❤️", "喜欢": "❤️",
    "心形": "❤️", "心脏": "❤️",
    "笑": "😄", "大笑": "😄", "笑脸": "😄", "呵呵": "😄",
    "微笑": "🙂", "微笑脸": "🙂",
    "哭": "😢", "哭脸": "😢", "难过": "😢", "悲": "😢", "沮丧": "😢",
    "惊讶": "😮", "吃惊": "😮", "哇": "😮",
    "感谢": "🙏", "谢谢": "🙏", "拜托": "🙏", "祈祷": "🙏",
    "鼓掌": "👏", "拍手": "👏", "太棒了": "👏",
    "握手": "🤝", "庆祝": "🎉", "派对": "🎉", "派对脸": "🥳",
    "蛋糕": "🎂", "冰淇淋": "🍦", "披萨": "🍕", "咖啡": "☕",
    "火": "🔥", "火焰": "🔥", "火箭": "🚀",
    "星星": "⭐", "闪光": "✨", "星星眼": "🤩", "彩虹": "🌈",
    "灯泡": "💡", "电灯泡": "💡",
    "对": "✅", "正确": "✅", "对号": "✅",
    "错": "❌", "不对": "❌", "叉号": "❌",
    "警告": "⚠️", "注意": "⚠️",
    "问号": "❓", "重要": "❗", "感叹号": "❗",
    "调皮": "😜", "酷": "😎", "思考": "🤔", "思考脸": "🤔",
    "汗": "😅", "皱眉": "😟", "皱眉脸": "😟",
    "生气": "😠", "困": "😴",
    "加油": "💪", "强壮": "💪", "肌肉": "💪", "举手": "🙋",
    "礼物": "🎁", "眼睛": "👀",
    #  ── 从真实缓存里见过的（反应区用到的）──
    "十二点": "🕛", "凤凰": "🐦", "高压": "🤯", "独角兽头": "🦄",
    "足球": "⚽", "篮球": "🏀", "奖杯": "🏆", "跳舞": "🕺",
    "橙心": "🧡", "黄心": "💛", "绿心": "💚", "蓝心": "💙",
    "紫心": "💜", "黑心": "🖤",
}

#  ★★★ 频道直达链接（实测最优）
#
#  教训：三种写法看着都合理，实际只有第三种一步到位。
#
#     ①  https://teams.cloud.microsoft/l/channel/{id}
#         → 被拦到中转页 dl/launcher/launcher.html（「加入对话」），
#           得再点一下「改用 Web 应用」才能进去（多花约 5 秒）。
#
#     ②  https://teams.cloud.microsoft/l/team/{t}/channel/{c}
#         → 同样被拦，不划算。
#
#     ③  https://teams.cloud.microsoft/_#/l/channel/{id}      ← 用这个
#         → 直接落到频道，9 秒内读到消息，实测选中项正确。
#           那个 ``_#`` 是 Teams 自己的内部路由前缀（就是中转页
#           url= 参数里解出来的形式）。
CHANNEL_URL = HOME_URL + "_#/l/channel/"

#  兼容用：中转页形式（万一将来 _# 失效，可回退到「进中转页再点按钮」）
CHANNEL_URL_FALLBACK = HOME_URL + "l/channel/"

#  单频道最多往回滚几轮
#
#  ★ 这个数从 14 提到 40 ——
#    实测「3 条就撑满 1091px」的频道很常见（消息本身很长），
#    所以要滚的轮数得按**内容**算，不能按条数算。
#    真正什么时候停由「连续几轮没有新消息」决定（见 STAGNANT_LIMIT），
#    这个上限只是兜底，防止某个频道无限滚。
DEFAULT_SCROLL_ROUNDS = 40

#  深度归档（补全历史）用的参数 —— 一次滚到最早，宁可慢
DEEP_SCROLL_ROUNDS = 160
DEEP_CHANNEL_BUDGET_SEC = 420
DEEP_TOTAL_BUDGET_SEC = 5400

#  连续多少轮没有新消息就认为「到头了」
STAGNANT_LIMIT = 3
#  单频道抓取总时限（秒）—— 防止某个频道卡住整个流程
CHANNEL_BUDGET_SEC = 120
#  单次全量抓取总时限（秒）。
#  17 个频道实测跑 280 秒：枚举阶段约 70 秒（要逐个进团队），
#  取数阶段每个频道 8~20 秒（看消息多少）。
TOTAL_BUDGET_SEC = 900


# ══════════════════════════════════════════════════════════
#  JS：公共点击
# ══════════════════════════════════════════════════════════

#  React / Fluent 只认完整的指针事件序列，单发一个 click 会被忽略。
#  必须按 pointerdown → mousedown → pointerup → mouseup → click 走一遍。
#
#  ★★ 每个函数都要**自带**这段逻辑，绝不抽出来挂到 window 上。
#    原因：Teasm/Teams 会做整页导航，一旦页面重载，注入到 window 的
#    东西就没了 —— 后续调用直接报 `CLICK is not defined`。
#    （这是实际踩到的坑，不是理论担心。）
_JS_CLICK_IMPL = r"""
  const __click = (el) => {
    if (!el) return false;
    try { el.scrollIntoView({block: 'center', inline: 'center'}); } catch (e) {}
    const r = el.getBoundingClientRect();
    const cx = r.x + r.width / 2, cy = r.y + r.height / 2;
    const mk = t => new MouseEvent(t, {
      bubbles: true, cancelable: true, view: window, button: 0, buttons: 1,
      clientX: cx, clientY: cy,
    });
    try { el.focus && el.focus(); } catch (e) {}
    for (const t of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
      try { el.dispatchEvent(mk(t)); } catch (e) {}
    }
    return true;
  };
"""

#  独立的点击函数（给外部工具用；内部一律用自包含版本）
JS_CLICK = "(el) => {" + _JS_CLICK_IMPL + "  return __click(el);\n}"

# ══════════════════════════════════════════════════════════
#  JS：状态探测
# ══════════════════════════════════════════════════════════

JS_STATE = r"""
() => {
  //  当前选中的频道名（左侧高亮项）
  let sel = '';
  document.querySelectorAll('[data-tid="channel-list-item"]').forEach(el => {
    const inner = el.querySelector('[data-tid^="channel-list-item-text-"]');
    if (el.getAttribute('aria-selected') === 'true'
        || el.closest('[aria-selected="true"]')) {
      sel = inner ? inner.innerText : (el.innerText || '');
    }
  });

  /*  团队卡数量。
      ★ 2026-09-25 实测：旧版 `-team-card` 已不存在，
        新版团队列表渲染在 [data-tid="app-layout-area--main"] 里，
        行上没有稳定 tid。这里用「行数」作估算：
        统计 main 区里「文字短、不像小标题/计数」的叶子行。
        仍优先用旧选择器（万一回滚）。 */
  let cards = document.querySelectorAll('[data-tid$="-team-card"]').length;
  if (!cards) {
    const scope = document.querySelector('[data-tid="app-layout-area--main"]');
    if (scope) {
      const SKIP = /^(课堂|团队|Class|Team|Teams|所有团队|加入或创建团队)$/i;
      const COUNT = /^\d+\s*(teams?|个团队)$/i;
      const seen = new Set();
      scope.querySelectorAll('*').forEach(el => {
        if (el.children.length > 2) return;
        const t = (el.innerText || '').replace(/\s+/g, ' ').trim();
        if (!t || t.length > 80) return;
        if (SKIP.test(t) || COUNT.test(t)) return;
        seen.add(t);
      });
      cards = seen.size;
    }
  }

  return {
    url: location.href.slice(0, 130),
    title: (document.title || '').slice(0, 90),
    launcher: location.href.indexOf('launcher') >= 0,
    cards: cards,
    chans: document.querySelectorAll('[data-tid="channel-list-item"]').length,
    msgs: document.querySelectorAll('[data-tid="channel-pane-message"]').length,
    pane: !!document.querySelector('[data-tid="message-pane-body"]'),
    empty: !!document.querySelector('[data-tid="channel-empty-state"]'),
    selected: (sel || '').trim(),
  };
}
"""

#  ★★ 2026-09-25 实测：Teams 改版后 `[data-tid$="-team-card"]` **已经不存在**。
#     现在团队列表渲染在 `[data-tid="app-layout-area--main"]` 里，
#     结构是「小标题（课堂 / 团队）+ 若干行」，行上只有文字，
#     没有稳定的 data-tid。所以这里改成：
#       ① 先试旧选择器（万一回滚了）
#       ② 没有就按新结构读：把 main 区按行切开，
#          取「有文字、不像小标题、不像计数」的行。
JS_TEAMS = r"""
() => {
  const out = [];
  /*  ① 旧版：带 -team-card 的卡片 */
  document.querySelectorAll('[data-tid$="-team-card"]').forEach(el => {
    const id = (el.getAttribute('data-tid') || '').replace('-team-card', '');
    const name = (el.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 80);
    if (id && name) out.push({id: id, name: name});
  });
  if (out.length) return out;

  /*  ② 新版：main 区里的行 */
  const scope = document.querySelector('[data-tid="app-layout-area--main"]');
  if (!scope) return out;
  const SKIP = /^(课堂|团队|Class|Team|Teams|加入或创建团队|所有团队|隐藏频道)$/i;
  const COUNT = /^\d+\s*(teams?|个团队)$/i;
  /*  行 = 叶子节点且文字短，或者带 role 的可点项 */
  const seen = new Set();
  scope.querySelectorAll('*').forEach(el => {
    if (el.children.length > 2) return;
    const t = (el.innerText || '').replace(/\s+/g, ' ').trim();
    if (!t || t.length > 80) return;
    if (SKIP.test(t) || COUNT.test(t)) return;
    /*  只要最内层那个（避免父子重复） */
    if (el.querySelector('*') && el.innerText.trim() === t &&
        el.children.length === 1 && el.firstElementChild.innerText.trim() === t) return;
    if (seen.has(t)) return;
    seen.add(t);
    /*  id 用名字兜底（新版拿不到 id；调用方会按名字找） */
    out.push({id: '', name: t});
  });
  return out;
}
"""

#  ★ 点开团队：新版没有 id 可依，改成**按名字点**（调用方优先传名字）。
#     旧版仍支持传 id。
JS_OPEN_TEAM = "(key) => {" + _JS_CLICK_IMPL + r"""
  const k = String(key || '').toLowerCase();
  /*  ① 旧版：按 id 找卡片 */
  if (k && k.length > 20) {
    const el = document.querySelector(`[data-tid="${key}-team-card"]`);
    if (el) return __click(el);
  }
  /*  ② 新版：在 main 区按名字找最内层可点元素 */
  const scope = document.querySelector('[data-tid="app-layout-area--main"]')
             || document.body;
  const cands = [...scope.querySelectorAll('*')].filter(el => {
    const t = (el.innerText || '').replace(/\s+/g, ' ').trim();
    return t.toLowerCase() === k && el.children.length <= 3;
  });
  if (!cands.length) {
    /*  退一步：包含匹配（团队名可能带后缀） */
    const loose = [...scope.querySelectorAll('*')].filter(el => {
      const t = (el.innerText || '').replace(/\s+/g, ' ').trim();
      return t.toLowerCase().includes(k) && t.length < 90 && el.children.length <= 3;
    });
    if (!loose.length) return false;
    return __click(loose[loose.length - 1]);
  }
  /*  取最深那个（最内层的可点元素） */
  return __click(cands[cands.length - 1]);
}"""

#  回到「所有团队」列表。
#  ★ 实测：直接 goto 同一域名**不会**重置 SPA 状态，
#    真正管用的是点左上角那个「所有团队」按钮。
#  ★ 2026-09-25 实测：新版把字号/位置改了，x < 260 这个门槛太紧，
#    而且按钮文字可能是「所有团队」或「所有 Teams」。放宽到 340。
JS_BACK_TO_TEAMS = "() => {" + _JS_CLICK_IMPL + r"""
  const found = [...document.querySelectorAll('button,a,[role="button"]')].find(x => {
    const r = x.getBoundingClientRect();
    const t = (x.innerText || '').replace(/\s+/g, '');
    return r.width > 40 && r.height > 14 && r.x < 340
        && (t.includes('所有团队') || t.includes('所有Teams'));
  });
  if (!found) return {how: 'none', ok: false};
  return {how: 'all-teams-btn', ok: __click(found)};
}"""

JS_CHANNELS = r"""
() => [...document.querySelectorAll('[data-tid="channel-list-item"]')]
  .map(el => {
    const inner = el.querySelector('[data-tid^="channel-list-item-text-"]');
    const id = inner
      ? (inner.getAttribute('data-tid') || '').replace('channel-list-item-text-', '')
      : '';
    return {
      id: id,
      name: (inner ? inner.innerText : el.innerText || '').trim(),
      h: Math.round(el.getBoundingClientRect().height),
    };
  })
  .filter(c => c.id && c.h > 6 && c.name)
"""

# ══════════════════════════════════════════════════════════
#  JS：读消息（已实测有效）
# ══════════════════════════════════════════════════════════

JS_READ = r"""
() => {
  const q = (r, t) => r.querySelector(`[data-tid="${t}"]`);
  const pane = q(document, 'message-pane-body');
  if (!pane) return {ok: false, why: 'no-pane'};
  const runway = q(pane, 'channel-pane-runway');
  if (!runway) return {ok: false, why: 'no-runway'};

  //  Teams 的时间戳有好几种写法：
  //     9/2 9:40      本年同月同日
  //     9/11 16:26    同上
  //     昨天 13:34    相对日期
  //     星期五 19:44   本周内
  //     2025/9/2 9:40 完整日期
  const RE_TIME = /(?:^|\s)(\d{1,2}\/\d{1,2}(?:\/\d{2,4})?\s+\d{1,2}:\d{2}(?:\s*[AP]M)?|昨天\s*\d{1,2}:\d{2}|星期[一二三四五六日天]\s*\d{1,2}:\d{2}|\d{4}\/\d{1,2}\/\d{1,2}\s+\d{1,2}:\d{2})\s*(已编辑|正在编辑)?/;

  // ────────────────────────────────────────────────────────────
  //  取文本（保留 emoji）
  //
  //  不用 innerText：Teams 的表情是 <img alt="好的">，
  //  innerText 会把 alt 当文字读出来，连成
  //  「好的 好的 好的」这种谁也看不懂的东西。
  //
  //  这里自己走一遍节点：
  //    · <img> 是表情 → 换真 emoji 字符
  //    · <img> 是别的 → 只留一个空格（不吐 alt）
  //    · 块级元素 → 补换行，段落不粘在一起
  // ────────────────────────────────────────────────────────────

  //  中文 alt → 真 emoji。只列消息里真会出现的常用几个，
  //  查不到就走下面的兜底（alt 本身就是 emoji 字符的情况）。
  const EMO = {
    '好的':'👍','赞':'👍','同意':'👍','支持':'👍','顶':'👍',
    '心':'❤️','红心':'❤️','爱心':'❤️','喜欢':'❤️',
    '笑':'😄','大笑':'😄','笑脸':'🙂','微笑':'🙂','呵呵':'😄',
    '哭':'😢','难过':'😢','悲':'😢',
    '惊讶':'😮','吃惊':'😮','哇':'😮',
    '感谢':'🙏','谢谢':'🙏','拜托':'🙏','祈祷':'🙏',
    '鼓掌':'👏','拍手':'👏','太棒了':'👏',
    '握手':'🤝','庆祝':'🎉','派对':'🎉','蛋糕':'🎂',
    '火':'🔥','火箭':'🚀','星星':'⭐','闪光':'✨',
    '灯泡':'💡','对':'✅','正确':'✅','错':'❌','不对':'❌',
    '警告':'⚠️','注意':'⚠️','问号':'❓','重要':'❗',
    '微笑脸':'🙂','调皮':'😜','酷':'😎','思考':'🤔',
    '汗':'😅','皱眉':'😟','生气':'😠','困':'😴',
    '加油':'💪','强壮':'💪','心脏':'❤️','礼物':'🎁',
    //  ↓↓↓ 从真实缓存里补的（反应区见过的名字）↓↓↓
    '大笑':'😄','微笑':'🙂','爱心':'❤️','电灯泡':'💡',
    '十二点':'🕛','凤凰':'🐦','高压':'🤯','沮丧':'😢',
    '独角兽头':'🦄','足球':'⚽','篮球':'🏀','奖杯':'🏆',
    '火箭':'🚀','火焰':'🔥','星星':'⭐','彩虹':'🌈',
    '鼓掌':'👏','举手':'🙋','肌肉':'💪','眼睛':'👀',
    '思考脸':'🤔','皱眉脸':'😟','哭脸':'😢','笑脸':'😄',
    '咖啡':'☕','披萨':'🍕','蛋糕':'🎂','冰淇淋':'🍦',
    '握手':'🤝','祈祷':'🙏','派对脸':'🥳','星星眼':'🤩',
    '心形':'❤️','红心':'❤️','橙心':'🧡','黄心':'💛',
    '绿心':'💚','蓝心':'💙','紫心':'💜','黑心':'🖤',
    '大拇指':'👍','向下':'👎','好的':'👍','不行':'👎',
    '对号':'✅','叉号':'❌','问号':'❓','感叹号':'❗',
  };

  //  ────────────────────────────────────────────────────────────
  //  反应区（reactions）—— 这是本轮真正要修的
  //
  //  Teams 的反应会长这样（屏幕阅读器文本 + 可见数量）：
  //      <div data-tid="reactions-bar">
  //        <button aria-label="3 个赞表情符号。">
  //          <img alt="赞" src="...emoticons...">
  //          <span class="sr-only">3 个赞表情符号。</span>
  //          <span>3</span>
  //        </button>
  //      </div>
  //
  //  两样东西都会混进正文：隐藏文本「3 个赞表情符号。」和可见的「3」。
  //  所以整块跳过，另外单独读成结构数据。
  //  ────────────────────────────────────────────────────────────

  //  反应容器的特征（data-tid / class 都试）
  const RE_BOX = /reaction|emoticon[-_]?bar|feedback[-_]?(bar|area)|message[-_]?reactions/i;

  //  屏幕阅读器专用元素的特征 —— 这类内容设计上就不该显示给人看
  const RE_SR = /(^|[-_ ])(sr|visually)[-_ ]?(only|hidden)|screen[-_]?reader|hidden[-_]?text|a11y[-_]?hidden/i;

  const isReBox = n => {
    const tid = (n.getAttribute && n.getAttribute('data-tid')) || '';
    const cls = (typeof n.className === 'string' && n.className) || '';
    return RE_BOX.test(tid) || RE_BOX.test(cls);
  };

  const isSrOnly = n => {
    const cls = (typeof n.className === 'string' && n.className) || '';
    if (RE_SR.test(cls)) return true;
    //  aria-hidden 的也是「别读我」
    if (n.getAttribute && n.getAttribute('aria-hidden') === 'true') return true;
    //  内联样式里写死隐藏的（Teams 有时这么干）
    const st = (n.getAttribute && n.getAttribute('style')) || '';
    return /display\s*:\s*none|visibility\s*:\s*hidden/i.test(st);
  };

  //  最后一道：万一结构没猜中，从文本里把这几句洗掉
  //
  //  要删的形态（真实数据里都有）：
  //      「3 个赞表情符号。」
  //      「2 个十二点表情符号。 2」   ← 后面那个裸数字是数量徽章
  //      「1 个大笑表情符号。」
  const cleanJunk = s => s
    //  ① 「N 个X表情符号。」+ 紧跟的裸数字
    .replace(/\d+\s*个\s*[^\s。，,]{0,16}?表情符号\s*[。.]\s*\d*/g, ' ')
    //  ② 只剩「N 个表情符号」的简写形态
    .replace(/\d+\s*个\s*表情符号\s*[。.]?/g, ' ')
    //  ③ 压掉因此产生的多余空格
    .replace(/[ \t]{2,}/g, ' ')
    .replace(/\s+([，,。.！!？?])/g, '$1')
    .trim();

  const BLOCK = /^(P|DIV|BR|LI|TR|H[1-6]|BLOCKQUOTE|PRE|SECTION|ARTICLE)$/;

  const T = el => {
    if (!el) return '';
    let out = '';
    const walk = n => {
      if (n.nodeType === 3) {                        // 文本
        out += n.nodeValue || '';
        return;
      }
      if (n.nodeType !== 1) return;                   // 注释等，忽略

      const tag = n.tagName;

      //  附件区整块跳过（文件名该单独收，不该混进正文）
      const tid = n.getAttribute && n.getAttribute('data-tid');
      if (tid && /attachment|file-card|file-attachment/.test(tid)) return;

      //  反应区整块跳过 —— 那里有「N 个X表情符号。」这种读屏文本，
      //  还有可见的数量徽章，两样都不该混进正文。
      //  （反应单独读成结构数据，见下面 readReactions）
      if (isReBox(n)) return;

      //  屏幕阅读器专用元素跳过
      if (isSrOnly(n)) return;

      if (tag === 'IMG') {
        const src = (n.getAttribute('src') || '').toLowerCase();
        const alt = (n.getAttribute('alt') || '').trim();
        const isEmo = /emoticon|emoji|reaction|\.gif/.test(src)
                   || n.getAttribute('data-tid') === 'emoticon-img'
                   //  Teams 的 emoji CDN 路径里带 /emoticons/
                   || /\/emoticons?\//.test(src);
        if (isEmo) {
          //  ① 先查表
          if (EMO[alt]) { out += EMO[alt]; return; }
          //  ② alt 本身就是 emoji 字符（有些语言版本这样）
          if (alt && !/^[\w\s:：:\u4e00-\u9fa5]{2,}$/.test(alt)) {
            out += alt;
            return;
          }
          //  ③ 认不出就丢掉 —— 显示「好的」比不显示更糟
          return;
        }
        out += ' ';                                   // 普通图片占个位
        return;
      }

      const block = BLOCK.test(tag);
      if (block && out && !out.endsWith('\n')) out += '\n';
      for (const c of n.childNodes) walk(c);
      if (block && out && !out.endsWith('\n')) out += '\n';
    };
    for (const c of el.childNodes) walk(c);
    //  收尾：压掉多余空行 + 洗掉漏网的反应文本
    return cleanJunk(out.replace(/\n{3,}/g, '\n\n'));
  };

  //  ── 把反应读成结构数据 ──
  //  返回 [{emoji:'👍', n:3}, ...]，读不到就是空数组。
  //
  //  做法：在反应容器里找表情图，顺着往上找「带数字的按钮」拿数量。
  //  数量拿不到就按 1 算 —— 至少 emoji 是对的。
  const readReactions = root => {
    const out = [];
    if (!root) return out;

    //  ★ 要找**全部**反应容器，不能找到一个就 break ——
    //    一条消息下面可能挂着好几个（赞、大笑、爱心…各一个），
    //    只读第一个会漏掉后面那些。
    const boxes = [];
    const scan = n => {
      if (n.nodeType !== 1) return;
      if (isReBox(n)) { boxes.push(n); return; }   // 命中就不再往里找
      for (const c of n.children) scan(c);
    };
    scan(root);
    if (!boxes.length) return out;

    boxes.forEach(box => {
      box.querySelectorAll('img').forEach(im => {
        const src = (im.getAttribute('src') || '').toLowerCase();
        const alt = (im.getAttribute('alt') || '').trim();
        const isEmo = /emoticon|emoji/.test(src) || /\/emoticons?\//.test(src);
        if (!isEmo) return;

        const ch = EMO[alt] || (/^\p{Emoji}/u.test(alt) ? alt : '');
        if (!ch) return;

        //  数量：从图片往上找 4 层，看有没有「1-4 位数字」的文本
        let n = 0;
        let up = im;
        for (let k = 0; k < 4 && up; k++) {
          up = up.parentElement;
          if (!up) break;
          //  读屏文本里也带数量：「3 个赞表情符号。」
          const m2 = (up.getAttribute('aria-label') || '')
                       .match(/(\d{1,4})\s*个/);
          if (m2) { n = parseInt(m2[1], 10); break; }
          const txt = (up.innerText || '').replace(/\s+/g, '');
          const m = txt.match(/^(\d{1,4})/);
          if (m) { n = parseInt(m[1], 10); break; }
        }
        out.push({ emoji: ch, n: n || 1 });
      });
    });

    //  同一个表情合并计数
    const merged = [];
    for (const r of out) {
      const hit = merged.find(x => x.emoji === r.emoji);
      if (hit) hit.n += r.n;
      else merged.push({ ...r });
    }
    return merged;
  };

  const hdr = el => {
    if (!el) return {author: '', time: '', edited: ''};
    const t = (el.innerText || '').replace(/\s+/g, ' ').trim();
    const m = t.match(RE_TIME);
    if (!m) return {author: t.replace(/^\d+\s+/, '').trim(), time: '', edited: ''};
    const idx = t.indexOf(m[1]);
    const a = t.slice(0, idx).trim()
                .replace(/^\d+\s+/, '')
                .replace(/^(上次阅读|新帖子|你|回复)\s*/, '').trim();
    return {author: a, time: m[1], edited: m[2] || ''};
  };

  const posts = [];
  [...runway.children].forEach(box => {
    if (box.tagName !== 'DIV') return;
    const msg = q(box, 'channel-pane-message') || box;
    const h = hdr(q(msg, 'post-message-subheader'));

    //  ★ 用 T() 而不是 innerText ——
    //    innerText 会把表情图片的 alt 当文字吐出来
    //    （三个表情拼成「好的 好的 好的」那种）。
    const tEl = msg.querySelector('h1,h2,h3');
    const title = tEl ? T(tEl).trim() : '';
    const paras = [];
    msg.querySelectorAll('p').forEach(p => {
      const t = T(p).replace(/[ \t]+/g, ' ').trim();
      if (t) paras.push(t);
    });
    if (!h.author && !title && !paras.length) return;

    const replies = [];
    const rs = q(msg, 'response-surface');
    if (rs) {
      rs.querySelectorAll('[data-tid="reply-message-header"]').forEach((rh, j) => {
        const h2 = hdr(rh);

        //  ── 找「这一条回复」的正文 ──
        //  ★ 早先这里只判断「比作者行长出一截」（+6 个字符），
        //    短回复（一两个字 + 表情）长不出那 6 个字符，
        //    于是一路走到最外层，把**后面别人的回复**也收进来。
        //    实测复现过：R1 的正文里出现了 R2 的作者名和内容。
        //
        //  现在按「不许圈进别的回复」来走：一旦上一层里出现
        //    第二个 reply-message-header，说明再往外就是整个
        //    回复区了，必须停。
        //
        //  Teams 有改版过，两种结构都要照顾：
        //    A. 每条回复有包裹层 —— header 和正文同一个父节点
        //    B. 没有包裹层 —— header 和正文是兄弟节点
        let cont = rh;
        for (let k = 0; k < 4 && cont.parentElement; k++) {
          const up = cont.parentElement;
          if (up.querySelectorAll('[data-tid="reply-message-header"]').length > 1) {
            break;                    // 再往外就不止这一条了
          }
          cont = up;
          if (T(cont).replace(T(rh), '').replace(/\s+/g, '').length) break;
        }

        const pick = n => T(n).replace(/[ \t]+/g, ' ').trim();

        let body = pick(cont);
        body = body.replace(pick(rh), '')
                   .replace(RE_TIME, ' ').replace(/\s+/g, ' ').trim();

        //  结构 B 的兜底：正文其实在 header 的下一个兄弟节点上
        if (!body) {
          const sib = rh.nextElementSibling;
          if (sib && !sib.matches('[data-tid="reply-message-header"]')) {
            body = pick(sib).replace(RE_TIME, ' ')
                            .replace(/\s+/g, ' ').trim();
          }
        }

        replies.push({author: h2.author, time: h2.time,
                      edited: h2.edited, text: body.slice(0, 8000),
                      reactions: readReactions(cont)});
      });
    }

    posts.push({
      author: h.author, time: h.time, edited: h.edited,
      title: title, text: paras.join('\n'),
      replies: replies, replyCount: replies.length,
      //  反应（表情 + 数量）。读不到就是空数组。
      reactions: readReactions(msg),
    });
  });

  return {
    ok: true,
    channel: (document.title || '').split('|').map(s => s.trim())[2] || '',
    posts: posts,
    nPosts: posts.length,
    nReplies: posts.reduce((a, b) => a + b.replies.length, 0),
    empty: !!q(pane, 'channel-empty-state'),
  };
}
"""

JS_SCROLL_UP = r"""
() => {
  const pane = document.querySelector('[data-tid="message-pane-body"]');
  if (!pane) return null;
  const vp = pane.querySelector('[data-tid="channel-pane-viewport"]') || pane;
  const before = vp.scrollTop;
  const step = Math.max(240, vp.clientHeight * 0.9);
  vp.scrollTop = Math.max(0, before - step);
  //  有些虚拟列表只认 wheel 事件，不认 scrollTop 赋值
  try {
    vp.dispatchEvent(new WheelEvent('wheel',
      {deltaY: -step, bubbles: true, cancelable: true}));
  } catch (e) {}
  return {before: before, after: vp.scrollTop, atTop: vp.scrollTop <= 1};
}
"""
#  ↑ JS_SCROLL_UP 已**弃用**：实测 `scrollTop` 赋值对 Teams 的虚拟列表无效，
#    现在统一走 teams_scroll.wheel_page()（CDP 真滚轮事件）。
#    保留这段字符串只为留个对照，代码里没有任何调用点。
#    若确认不再需要，可连同本段一起删（见 _tools/check_api_surface.py 的思路：
#    死代码应当可见，而不是靠猜）。


# ══════════════════════════════════════════════════════════
#  JS：读附件卡
#
#  ★★ 这一块是**实测踩出来的**，不是照文档写的。
#
#  ① 附件卡是**懒挂载**的。
#     初始 DOM 里只有一个空壳：
#         <div data-slot-name:r2m:="root"></div>
#     光是 scrollIntoView **不够** —— 得让它真的被**绘制**一次，
#     内容才会挂进去。`Page.captureScreenshot` 能强制一次绘制。
#
#  ② 文件名和链接在一起，用**换行**分隔：
#         "EC Roster 9.18.pdf\nhttps://beijing101-my.sharepoint.com/:b:/…"
#     所以取 aria-label / title 整串，Python 侧再拆
#     （见 `sp_download.link_from_label`）。
#
#  ③ 附件网格（`data-tid*="file-attachment-grid"`）**不等于**消息。
#     一个消息可能有多个网格。所以按「消息盒子」分组，逐盒子收集，
#     这样才知道哪个附件属于哪条消息。
# ══════════════════════════════════════════════════════════

#  ① 数一数有多少个还没挂载的网格（决定要不要逼绘制）
JS_ATTACH_COUNT = r"""
() => {
  const pane = document.querySelector('[data-tid="message-pane-body"]');
  if (!pane) return {grids: 0, mounted: 0};
  const grids = pane.querySelectorAll('[data-tid*="file-attachment-grid"]');
  let mounted = 0;
  grids.forEach(g => {
    if (g.querySelector('[aria-label],[title]')) mounted++;
  });
  return {grids: grids.length, mounted: mounted};
}
"""

#  ② 把第 i 个网格滚进视口（逼挂载前先让它可见）
JS_ATTACH_SHOW = r"""
(i) => {
  const pane = document.querySelector('[data-tid="message-pane-body"]');
  if (!pane) return false;
  const g = pane.querySelectorAll('[data-tid*="file-attachment-grid"]')[i];
  if (!g) return false;
  try {
    g.scrollIntoView({block: 'center', inline: 'nearest'});
  } catch (e) {}
  return true;
}
"""

#  ③ 读所有附件。按消息盒子分组，这样能对上「哪个附件属于哪条消息」。
#
#  ★ 只收「像文件名」的东西（含 2~5 位扩展名），
#    不然会把 aria-label 里的说明文字（「3 个文件」之类）也当文件名。
JS_ATTACH = r"""
() => {
  const q = (r, t) => r.querySelector(`[data-tid="${t}"]`);
  const pane = q(document, 'message-pane-body');
  if (!pane) return {ok: false, why: 'no-pane'};
  const runway = q(pane, 'channel-pane-runway');
  if (!runway) return {ok: false, why: 'no-runway'};

  //  「像文件名」：末段是 2~5 位的字母数字扩展名
  const RE_FILE = /\.[A-Za-z0-9]{2,5}(?:$|\s)/;
  //  明显不是文件的（图标名、状态文本）
  const RE_SKIP = /^(image|icon|svg|png_|undefined|null|true|false)/i;

  const collect = root => {
    const out = [];
    const push = v => {
      const s = String(v || '').trim();
      if (!s || s.length > 900) return;
      if (!RE_FILE.test(s)) return;
      if (RE_SKIP.test(s)) return;
      out.push(s);
    };
    root.querySelectorAll('[aria-label],[title],[data-tid]').forEach(c => {
      push(c.getAttribute('aria-label'));
      push(c.getAttribute('title'));
      //  有些版本把文件名放在 data-tid 的尾巴上
      const tid = c.getAttribute('data-tid') || '';
      if (RE_FILE.test(tid)) push(tid);
    });
    return [...new Set(out)];
  };

  const boxes = [];
  let total = 0;
  [...runway.children].forEach((box, idx) => {
    if (box.tagName !== 'DIV') return;
    const msg = q(box, 'channel-pane-message') || box;
    const grids = msg.querySelectorAll('[data-tid*="file-attachment-grid"]');
    if (!grids.length) return;
    const files = [];
    grids.forEach(g => {
      //  网格自己的 aria-label 有时直接写着文件名
      push_grid: {
        const ga = g.getAttribute('aria-label');
        if (ga && RE_FILE.test(ga)) files.push(ga);
      }
      for (const f of collect(g)) {
        if (!files.includes(f)) files.push(f);
      }
    });
    if (files.length) {
      total += files.length;
      boxes.push({idx: idx, n: files.length, files: files});
    }
  });

  return {ok: true, boxes: boxes, nFiles: total,
          nGrids: pane.querySelectorAll(
            '[data-tid*="file-attachment-grid"]').length};
}
"""


def force_paint(page) -> bool:
    """截一张图 —— 目的不是存图，是**逼浏览器绘制**。

    ★ 附件卡是懒挂载的：不绘制，`<div data-slot-name:r2m:="root">`
      就永远是个空壳，读不到文件名。这一下是整条链路的关键。

    失败不算错（有些环境不允许截图），返回 False 让调用方决定要不要重试。
    """
    try:
        page.cdp.send(
            "Page.captureScreenshot",
            {"format": "png", "captureBeyondViewport": False},
            session_id=page.sid, timeout=30,
        )
        return True
    except Exception as e:
        log.debug("逼绘制失败：%s", e)
        return False


def collect_attachments(page, *, tries: int = 5,
                        quiet: bool = False) -> dict:
    """把当前频道里所有附件卡的**文件名 + 链接**读出来。

    返回 ``{ok, items:[{name, url, idx}], nGrids, note?}``。

    ★ 为什么要「滚 → 画 → 读」循环若干次：
      网格是逐批挂载的，一次 `force_paint` 通常只挂出**当前视口**里那几个。
      所以每轮先滚下一个还没挂的格子，再逼绘制，再看总数有没有涨。
      涨了就继续，连续几轮不涨就收工。
    """
    from . import sp_download

    items: list[dict] = []
    seen: set = set()
    n_grids = 0
    stagnant = 0

    for rnd in range(max(1, tries)):
        try:
            cnt = page.eval(JS_ATTACH_COUNT) or {}
        except Exception as e:
            return {"ok": False, "items": [], "nGrids": 0,
                    "note": f"读附件网格失败：{str(e)[:100]}"}

        n_grids = int(cnt.get("grids") or 0)
        mounted = int(cnt.get("mounted") or 0)
        if n_grids <= 0:
            return {"ok": True, "items": [], "nGrids": 0,
                    "note": "这个频道没有附件"}

        #  逐个把没挂的格子滚进视口
        for i in range(n_grids):
            try:
                page.eval(f"({JS_ATTACH_SHOW})({i})")
            except Exception as e:
                #  单个格子没滚成功不致命（后面还会整体再读一次），
                #  但要留下痕迹：否则「附件少了几个」永远查不出原因
                log.debug("第 %d 个附件格滚入视口失败：%s", i, e)
        time.sleep(0.7)
        force_paint(page)
        time.sleep(0.8)

        try:
            d = page.eval(JS_ATTACH) or {}
        except Exception as e:
            return {"ok": False, "items": [], "nGrids": n_grids,
                    "note": f"读附件失败：{str(e)[:100]}"}

        before = len(items)
        for box in d.get("boxes") or []:
            idx = box.get("idx")
            for label in box.get("files") or []:
                got = sp_download.link_from_label(label)
                if not got:
                    continue
                key = (got["url"] or "", got["name"])
                if key in seen:
                    continue
                seen.add(key)
                items.append({"name": got["name"], "url": got["url"],
                              "idx": idx})
        added = len(items) - before
        if not quiet:
            log.info("第 %d 轮：%d 个网格（已挂 %d），收到 %d 个文件",
                     rnd + 1, n_grids, mounted, len(items))

        if added == 0:
            stagnant += 1
            if stagnant >= 2:
                break
        else:
            stagnant = 0

    note = ""
    if n_grids and not items:
        note = (f"有 {n_grids} 个附件网格，但没读出文件名 —— "
                f"附件卡可能一直没挂载（需要画面真的绘制一次）")
    return {"ok": True, "items": items, "nGrids": n_grids, "note": note}


def attach_to_posts(posts: list[dict], items: list[dict]) -> int:
    """把收集到的附件挂到对应的消息上（按 DOM 里的盒子序号对）。

    ★ 为什么要对号：`read_channel` 返回的 posts 与 DOM 里的消息盒子
      是同一个顺序（都来自 runway.children 的遍历），
      所以 `idx` 就能直接当 posts 的下标用。

    返回挂上了几条。
    """
    n = 0
    for it in items:
        i = it.get("idx")
        if not isinstance(i, int) or not (0 <= i < len(posts)):
            continue
        p = posts[i]
        lst = p.setdefault("attachments", [])
        if any(a.get("url") == it.get("url")
               and a.get("name") == it.get("name") for a in lst):
            continue
        lst.append({"name": it.get("name"), "url": it.get("url")})
        n += 1
    return n


# ══════════════════════════════════════════════════════════
#  DJ 序列化助手 / Python 侧工具
# ══════════════════════════════════════════════════════════

def _msg_key(m: dict) -> str:
    """一条消息的稳定标识（用于去重与合并）。"""
    return "|".join((
        (m.get("author") or "").strip(),
        (m.get("time") or "").strip(),
        (m.get("title") or "").strip()[:60],
        (m.get("text") or "").strip()[:80],
    ))


def merge_posts(new: list, old: list) -> list:
    """新数据与旧数据合并：以新为准，旧的补充在后面。

    这样即使某次只抓到部分（虚拟滚动没滚到头），
    历史累积的消息也不会丢。

    ★ 附件要**并集**，不能「新的没有就丢掉旧的」：
      附件只在它那条消息滚进视口时才收得到，
      所以「这次没收到」经常只是没滚到，不代表消息真的没附件了。
      直接以新盖旧 → 上一轮辛苦收到的 PDF 链接会被抹掉。
    """
    seen: dict = {}
    out: list = []
    for m in list(new) + list(old):
        if not isinstance(m, dict):
            continue
        k = _msg_key(m)
        if not k.strip("|"):
            continue
        if k in seen:
            #  重复的（旧的）只用来补附件，别的不动
            prev = seen[k]
            atts = prev.setdefault("attachments", [])
            have = {(a.get("url"), a.get("name")) for a in atts}
            for a in (m.get("attachments") or []):
                if (a.get("url"), a.get("name")) not in have:
                    atts.append(a)
                    have.add((a.get("url"), a.get("name")))
            continue
        seen[k] = m
        out.append(m)
    return out


def _wait_for(page, js: str, ok, tries: int = 25, gap: float = 1.0) -> dict:
    """轮询等条件成立。比固定 sleep 可靠 —— SPA 加载时间波动很大。"""
    last: dict = {}
    fails = 0
    for attempt in range(tries):
        try:
            last = page.eval(js) or {}
        except Exception as e:
            #  轮询期间页面正在重建，eval 偶尔失败是常态；
            #  但若每次都失败，这条记录就是「等不到条件」的唯一线索
            fails += 1
            log.debug("轮询求值失败（第 %d/%d 次）：%s", attempt + 1, tries, e)
            last = {}
        try:
            if ok(last):
                if fails:
                    log.debug("轮询第 %d 次成功（期间失败 %d 次）",
                              attempt + 1, fails)
                return last
        except Exception as e:
            log.debug("轮询条件判断出错（当作未满足）：%s", e)
        time.sleep(gap)
    if fails:
        log.warning("轮询等不到条件：共 %d 次里求值失败 %d 次（可能是页面一直没加载完）",
                    tries, fails)
    return last


def _guard(page, quiet: bool = True) -> None:
    """清场：中转页退回、横幅点掉。任何异常都不影响主流程。"""
    try:
        from . import teams_guard
        teams_guard.guard(page, HOME_URL, quiet=quiet)
    except Exception as e:
        log.debug("清场跳过：%s", e)


# ══════════════════════════════════════════════════════════
#  阶段 1：枚举团队 + 频道
# ══════════════════════════════════════════════════════════

def _go_home(page, tries: int = 3) -> bool:
    """回到团队列表页，等卡片出来。

    ★ 关键：Teams 是 SPA，``goto`` 同一域名**不会**重置状态
      （页面不重新加载，视图还停在原处）。
      真正管用的是点左上角的「所有团队」按钮 —— 实测 100% 有效。

      所以顺序是：先点按钮 → 不行再 goto → 每次之后都等卡片。
    """
    for attempt in range(tries):
        #  ① 先试着点「所有团队」（最可靠）
        try:
            r = page.eval(JS_BACK_TO_TEAMS)
            if isinstance(r, dict) and r.get("ok"):
                st = _wait_for(page, JS_STATE,
                               lambda v: v.get("cards", 0) > 0,
                               tries=12, gap=1.0)
                if st.get("cards", 0) > 0:
                    return True
        except Exception as e:
            log.debug("点「所有团队」失败：%s", e)

        #  ② 还在团队里 → goto 兜底（加了 r= 参数强制当成新导航）
        try:
            page.goto(HOME_URL if attempt == 0 else f"{HOME_URL}?r={attempt}",
                      timeout=40000)
        except Exception as e:
            log.debug("导航回首页失败：%s", e)
        _guard(page)
        st = _wait_for(page, JS_STATE, lambda v: v.get("cards", 0) > 0,
                       tries=20, gap=1.2)
        if st.get("cards", 0) > 0:
            return True

    return False


def enum_teams_and_channels(page, progress=None) -> list[dict]:
    """进每个团队收集频道列表。

    返回 ``[{id, name, channels:[{id, name}]}]``。

    这是唯一需要点击的阶段 —— 因为频道的完整列表只在进团队后
    才渲染出来（Teams 不提供「列出所有频道」的公开接口）。
    但每类只点一次，且不需要用户参与。
    """
    def say(msg: str) -> None:
        log.info(msg)
        if progress:
            try:
                progress(msg)
            except Exception:
                pass

    #  注：点击逻辑已内联进各个 JS 函数，不需要再往 window 上挂东西
    #  （挂了也会被页面重载冲掉，见 _JS_CLICK_IMPL 的注释）

    if not _go_home(page):
        say("打不开团队列表")
        return []

    teams = page.eval(JS_TEAMS) or []
    say(f"找到 {len(teams)} 个团队")

    out: list[dict] = []
    for t in teams:
        #  ★ 每次都要先回列表 —— 进过团队之后卡片列表会被替换掉，
        #    留在原地再查卡片会查不到（这是踩过的坑）。
        if not _go_home(page):
            say(f"「{t['name']}」回不去列表，跳过")
            continue

        #  ★ 新版 Teams 读不到团队 id（行上只有文字），
        #    所以优先按**名字**点；老版本传 id 也能走。
        opened = False
        if t.get("name"):
            opened = bool(page.call(JS_OPEN_TEAM, t["name"]))
        if not opened and t.get("id"):
            opened = bool(page.call(JS_OPEN_TEAM, t["id"]))
        if not opened:
            say(f"「{t['name']}」打不开，跳过")
            continue

        st = _wait_for(page, JS_STATE, lambda v: v.get("chans", 0) > 0,
                       tries=25, gap=1.2)
        if st.get("chans", 0) <= 0:
            say(f"「{t['name']}」没等到频道")
            out.append({"id": t["id"], "name": t["name"], "channels": []})
            continue

        chans = page.eval(JS_CHANNELS) or []
        say(f"「{t['name']}」{len(chans)} 个频道")
        out.append({
            "id": t["id"], "name": t["name"],
            "channels": [{"id": c["id"], "name": c["name"]} for c in chans],
        })

    return out


# ══════════════════════════════════════════════════════════
#  阶段 2：按频道 ID 抓消息
# ══════════════════════════════════════════════════════════

def _channel_page_ready(page) -> bool:
    """页面上到底有没有「内容」——用来区分「还没加载」和「真的没消息」。"""
    js = r"""
    (() => {
      const b = document.body;
      if (!b) return false;
      const t = (b.innerText || '').trim();
      if (t.length < 20) return false;
      return !!(document.querySelector('[data-tid="message-pane-body"]')
             || document.querySelector('[data-tid="channel-pane-runway"]')
             || document.querySelector('[data-tid="channel-list"]')
             || document.querySelector('[role="tab"]'));
    })()
    """
    try:
        return bool(page.eval(js))
    except Exception:
        return False


def _channel_is_login(page) -> bool:
    """当前是不是掉到微软登录页了。"""
    try:
        url = (page.eval("location.href") or "").lower()
    except Exception:
        return False
    return any(k in url for k in (
        "login.microsoftonline.com", "login.live.com",
        "login.microsoft.com", "adfs.", "/oauth2/"))


def _recover_channel(page, channel_id: str) -> str:
    """把频道页救回来。返回做过什么（空串 = 没动）。

    ★ 为什么需要它 ★
      以前 read_channel 只在「launcher 中转页」时才 guard。
      可是实测里更常见的是**页面空白**或**掉登录**：
      body 为空、只剩标题栏，URL 已经跳到 login.microsoftonline.com。
      这种情况下 JS_READ 找不到 message-pane-body，
      报出来的原因是 "no-pane" —— 把「浏览器没就绪」伪装成「频道结构不对」，
      然后这个频道就被判失败、数据一条都没有。

      用户不该为此再点一次按钮 —— 程序自己应该：
        回首页 → 等 Teams 就绪 → 清场 → 重新进频道。
    """
    from . import teams_guard, teams_reader

    acted = ""

    #  ① 掉登录 / 中转页：先回首页让它自己验一次会话
    if _channel_is_login(page):
        acted = "掉登录，回首页"
        try:
            page.goto(teams_reader.TEAMS_URL, timeout=45000)
        except Exception:
            pass
        try:
            teams_reader.wait_teams_ready(timeout=45, quiet=True)
        except Exception:
            pass

    #  ② 页面空白（Teams 还在启动 / 渲染挂了）：回首页重进更彻底
    if not acted and not _channel_page_ready(page):
        acted = "页面空白，回首页重进"
        try:
            page.goto(teams_reader.TEAMS_URL, timeout=45000)
            teams_reader.wait_teams_ready(timeout=45, quiet=True)
        except Exception:
            pass

    #  ③ 不管哪种，都清一次场（中转页横幅 / 弹窗），再回目标频道
    try:
        teams_guard.guard(page, teams_reader.TEAMS_URL, quiet=True)
    except Exception:
        pass
    try:
        page.goto(CHANNEL_URL + channel_id, timeout=45000)
    except Exception:
        pass
    return acted


def _fail_reason(page, fallback: str = "no-pane") -> str:
    """失败时给出**真实**原因，不要一律 no-pane。"""
    if _channel_is_login(page):
        return "需要重新登录 Teams（登录态掉了）"
    if not _channel_page_ready(page):
        return "Teams 页面没加载出来（空白页）"
    return fallback


def _teams_logged_in(page) -> bool:
    """Teams 现在是登录状态吗（能看见消息区/侧栏，而不是落地页）。"""
    js = r"""
    (() => {
      if (location.href || '').indexOf('login.microsoftonline.com') >= 0) return false;
      if (document.querySelector('[data-tid="message-pane-body"]')) return true;
      if (document.querySelector('[data-tid="channel-list"]')) return true;
      if (document.querySelector('[data-tid="app-bar"]')) return true;
      //  落地页特征：只有「登录 / 加入会议」
      const t = (document.body && document.body.innerText || '');
      if (/我们正在为你设置内容|登录\s*\n?\s*加入会议/.test(t)) return false;
      return false;
    })()
    """
    try:
        return bool(page.eval(js))
    except Exception:
        return False


def _ensure_teams_ready(page, say, allow_visible: bool = True) -> bool:
    """确保 Teams 处于已登录且界面就绪的状态。必要时**自己弹窗**让 SSO 完成。

    ★ 微软的交互式登录在无头浏览器里永远卡在「尝试使你登录」，
      所以掉登录时必须切可见窗口 —— 这不是让用户"再点一次按钮"，
      而是程序自己把窗口摆好、登录完自己接着抓。
    """
    from . import teams_reader

    #  ① 已经在就绪状态
    if _teams_logged_in(page):
        return True

    #  ② 回首页等一下（有时只是没渲染完）
    say("检查 Teams 登录态…")
    try:
        page.goto(teams_reader.TEAMS_URL, timeout=45000)
    except Exception:
        pass
    try:
        teams_reader.wait_teams_ready(timeout=45, quiet=True)
    except Exception:
        pass
    if _teams_logged_in(page):
        say("Teams 已就绪")
        return True

    #  ③ 掉登录 → 切可见窗口，让微软 SSO 自己完成（通常不用输密码）
    if allow_visible:
        say("Teams 登录态掉了，正在打开窗口恢复…（无需操作，等它自己完成）")
        #  如果日常浏览器里本来就是登录好的，提示可以复用，免得再登一次
        try:
            if not getattr(config, "BROWSER_REUSE_PROFILE", False):
                from . import browser as _b
                log.info("提示：若你日常浏览器里 Teams 是登录状态，可设 "
                         "MBPK_BROWSER_REUSE_PROFILE=1（并用 MBPK_BROWSER 指定浏览器）"
                         "直接复用登录态，当前浏览器=%s",
                         _b.display_name(_b.find()))
        except Exception:
            pass
        try:
            teams_reader.open_teams(headless=False)
        except Exception as e:
            log.info("打开可见窗口失败：%s", e)
        deadline = time.time() + 240
        while time.time() < deadline:
            time.sleep(6)
            try:
                page = teams_reader._get_page() or page      # noqa: SLF001
            except Exception:
                pass
            if _teams_logged_in(page):
                say("Teams 登录态已恢复")
                return True
        say("Teams 仍未登录：需要在弹出的窗口里用微软账号登录一次")

    return _teams_logged_in(page)


def read_channel(page, channel_id: str,
                 scroll_rounds: int = DEFAULT_SCROLL_ROUNDS,
                 budget_sec: int = CHANNEL_BUDGET_SEC,
                 known: set | None = None,
                 deep: bool = False) -> dict:
    """抓一个频道的消息（用深度链接直达，不点击）。

    ``known``：上次抓取的消息 key 集合（增量模式）。
    滚动途中发现「整屏都是已抓过的消息」就提前收工 ——
    新消息总是出现在最下面，追上历史之后继续翻只会读到旧数据。

    ``deep``：**深度归档**。忽略 known，一直滚到连续多轮没有新消息为止，
    用来把历史消息补进本地缓存（搜索要能搜到，就必须先有）。
    日常增量抓取不要开它 —— 一个频道可能要滚上百轮。

    返回 ``{ok, posts:[...], nPosts, nReplies, reason?}``
    """
    t0 = time.time()

    #  ★ 深度归档：不能因为「这屏都见过」就停 —— 那正是增量模式的做法，
    #    也正是它导致老消息进不来的原因。
    if deep:
        known = None

    #  ① 走快路径 `_#/l/channel/{id}`
    try:
        page.goto(CHANNEL_URL + channel_id, timeout=45000)
    except Exception as e:
        return {"ok": False, "reason": f"导航失败：{str(e)[:90]}",
                "posts": [], "nPosts": 0, "nReplies": 0}

    st = _wait_for_channel(page, tries=14)

    #  ★ 页面压根没起来（空白 / 掉登录）→ 自救一次再判
    #    实测：这里以前直接往下走，最后报 "no-pane"，看起来像频道结构问题，
    #    其实是浏览器没就绪。用户不该为此再点一次。
    if (not st.get("empty") and not st.get("launcher")
            and (st.get("msgs") or 0) <= 0 and not _channel_page_ready(page)):
        acted = _recover_channel(page, channel_id)
        if acted:
            log.info("频道页未就绪（%s），已自救：%s", channel_id[:18], acted)
        st = _wait_for_channel(page, tries=22)

    if st.get("launcher"):
        #  ★ 万一落到了「加入对话」中转页 → 点「改用 Web 应用」拉回网页版。
        #    需要兜底，因为 Teams 有时会忽略 `_#` 前缀。
        log.debug("落到中转页，改走备用路径")
        _guard(page)
        if not st.get("pane") and not st.get("chans"):
            try:
                page.goto(CHANNEL_URL_FALLBACK + channel_id, timeout=40000)
            except Exception:
                pass
            st = _wait_for_channel(page, tries=16)
            if st.get("launcher"):
                _guard(page)          # guard 会点「改用 Web 应用」
                st = _wait_for_channel(page, tries=10)

    #  ② 空频道（消息区显示「欢迎加入…」）算成功，不必等到有消息
    if st.get("empty"):
        return {"ok": True, "posts": [], "nPosts": 0, "nReplies": 0,
                "empty": True, "channel": st.get("selected", "")}

    if (st.get("msgs") or 0) <= 0:
        d = page.eval(JS_READ) or {}

        #  ★ 一次没读到就重来一次 —— Teams 加载时间波动很大，
        #    偶尔会「页面还在转圈」就超时了。实测有约 1/5 的频道会碰上，
        #    重试一次基本都能救回来（不重试就会白丢一个频道的数据）。
        if not d.get("ok") and not d.get("empty"):
            log.debug("频道没读到（%s），再试一次", d.get("why"))
            time.sleep(3)
            try:
                page.goto(CHANNEL_URL + channel_id, timeout=45000)
            except Exception:
                pass
            st = _wait_for_channel(page, tries=18)
            if st.get("empty"):
                return {"ok": True, "posts": [], "nPosts": 0, "nReplies": 0,
                        "empty": True, "channel": st.get("selected", "")}
            d = page.eval(JS_READ) or {}

        if d.get("empty"):
            return {"ok": True, "posts": [], "nPosts": 0, "nReplies": 0,
                    "empty": True, "channel": st.get("selected", "")}
        if not d.get("ok"):
            return {"ok": False,
                    "reason": _fail_reason(page, d.get("why") or "读不到消息区"),
                    "posts": [], "nPosts": 0, "nReplies": 0}

    #  ③ 往上滚，累积。
    #
    #  ★★ 这一段是这一版修的重点，说明白为什么这么写 ——
    #
    #  旧写法有两个错：
    #
    #   ① 用 JS 赋 `scrollTop` 来滚（`JS_SCROLL_UP`）。
    #      实测对 Teams 的虚拟列表**基本无效** ——
    #      虚拟列表把「当前范围」记在自己的状态里，
    #      赋 scrollTop 会被它覆盖回去。
    #      → 改成 `teams_scroll.wheel_page()`（CDP 真滚轮，
    #        `Input.dispatchMouseEvent`）。这条是通的。
    #
    #   ② 拿「scrollTop 有没有变小」当「滚动成功」的判据
    #      （`if scr.get('atTop') or before == after: break`）。
    #      这个判据**分不清两种情况**：
    #        · 已经滚到最早了（正常结束）
    #        · 这个容器压根不响应滚动（我们根本没动）
    #      而且虚拟列表到顶时 scrollTop 未必是 0。
    #      → 改成**只看内容**：连续 `STAGNANT_LIMIT` 轮
    #        没有新消息才算到头。
    #
    #  ★ 为什么必须「边滚边读」：
    #    虚拟列表会把滚出视口的消息**从 DOM 里回收掉**。
    #    所以不能「先滚到底、再读一次」—— 那样只能读到最后一屏。
    #    每一轮滚完立刻读、就地累积去重，才是对的。
    seen: set = set()
    posts: list[dict] = []
    stagnant = 0
    for _rnd in range(max(1, scroll_rounds)):
        if time.time() - t0 > budget_sec:
            break
        if _CANCEL.is_set():
            log.info("收到停止请求，频道 %s 提前收工（已读 %d 条）",
                     channel_id[:24], len(posts))
            break

        #  先读当前这一屏
        d = page.eval(JS_READ) or {}
        if not d.get("ok"):
            break
        added = 0
        screen_valid = 0          # 这一屏能算出稳定 key 的消息数
        screen_known = 0          # 其中上次就抓过的
        for m in d.get("posts") or []:
            k = _msg_key(m)
            if not k.strip("|"):
                continue                       # 空消息不算数
            screen_valid += 1
            if known and k in known:
                screen_known += 1
            if k in seen:
                continue
            seen.add(k)
            posts.append(m)
            added += 1

        #  增量模式：整屏都是上次抓过的 → 已追上历史，直接收工。
        #  （新消息在最下面；能读到一整屏旧消息，说明再往上只有更旧的。）
        if known and screen_valid > 0 and screen_known == screen_valid:
            log.debug("增量：整屏 %d 条都是旧消息，停（本轮新收 %d 条）",
                      screen_valid, len(posts))
            break

        if added == 0:
            stagnant += 1
            #  深度归档放宽到 6 轮：Teams 的虚拟列表在接近顶部时
            #  有时要等两三屏才把更早的消息挂出来，阈值太小会提前收工。
            limit = 6 if deep else STAGNANT_LIMIT
            if stagnant >= limit:
                break
        else:
            stagnant = 0

        #  再往里滚一屏（真滚轮）
        rolled = teams_scroll.wheel_page(page)
        if not rolled:
            #  滚轮都发不出去 → 别在这儿耗时间，直接收尾
            log.debug("滚轮发不出去，停止滚动（已收 %d 条）", len(posts))
            break
        time.sleep(1.35)

    #  滚动是「从新往旧」，反一下就接近时间顺序
    posts.reverse()

    #  ④ 收附件。
    #
    #  ★ 放在**滚完之后**单独做，而不是滚动循环里顺手收。
    #    原因：附件卡要「滚进视口 + 真的绘制一次」才挂载，
    #    在快速滚动的循环里做这件事会拖慢每一轮（实测每轮多 1.5 秒），
    #    而且滚动中的视口一直在变，挂载状态不稳定。
    #    滚完之后再专门收一遍，画面是静止的，成功率高得多。
    #
    #  ★★ 顺序很要紧：**先收附件，再 reverse**。
    #     DOM 里的消息盒子是「上面旧下面新」，
    #     而 collect_attachments() 给的 idx 就是**DOM 序号**，
    #     所以要在 posts 还是 DOM 顺序时对号入座。
    #     先 reverse 再挂 → 附件会挂到**对面**那条消息上（实测会错位）。
    #
    #  ★ 代价：虚拟列表会把滚出视口的消息回收掉，
    #    所以滚完之后只有「当前这一屏」的附件能收到。
    #    对 EC 频道够用（每天一两份名单，就在最近几条消息上）。
    attach_note = ""
    try:
        alt = collect_attachments(page, quiet=True)
        got = alt.get("items") or []
        if got:
            n_a = attach_to_posts(posts, got)
            attach_note = f"收附件 {n_a} 个"
            log.info("频道附件：%d 个（挂到 %d 条消息上）", len(got), n_a)
        elif alt.get("note"):
            attach_note = str(alt["note"])
    except Exception as e:
        log.debug("收附件失败：%s", e)
        attach_note = f"收附件失败：{str(e)[:60]}"

    #  滚动是「从新往旧」，反一下就接近时间顺序
    posts.reverse()

    return {
        "ok": True,
        "posts": posts,
        "nPosts": len(posts),
        "nReplies": sum(m.get("replyCount", 0) for m in posts),
        "channel": st.get("selected", ""),
        "attach_note": attach_note,
    }


def _wait_for_channel(page, tries: int = 14, gap: float = 1.2) -> dict:
    """等频道内容就绪。

    两种「就绪」都算数：
      · 有消息（msgs > 0）
      · 空频道提示（empty = true）—— 空频道本来就没消息，不该白等
    """
    last: dict = {}
    for _ in range(tries):
        try:
            last = page.eval(JS_STATE) or {}
        except Exception:
            last = {}
        if last.get("empty") or (last.get("msgs") or 0) > 0:
            return last
        if last.get("launcher"):
            return last              # 交给调用方处理
        time.sleep(gap)
    return last


# ══════════════════════════════════════════════════════════
#  对外主入口
# ══════════════════════════════════════════════════════════

def _prev_posts_by_channel(prev: dict) -> dict[str, list]:
    """上次抓取的 {频道 id: posts} 索引（增量/合并用）。"""
    out: dict[str, list] = {}
    for t in (prev or {}).get("teams") or []:
        for c in t.get("channels") or []:
            cid = c.get("id")
            posts = c.get("posts")
            if cid and isinstance(posts, list) and posts:
                out[cid] = posts
    return out


def crawl_all(scroll_rounds: int = DEFAULT_SCROLL_ROUNDS,
              total_budget: int = TOTAL_BUDGET_SEC,
              progress=None,
              keep_posts: bool = True,
              incremental: bool | None = None,
              deep: bool = False) -> dict:
    """全量抓取：所有团队 → 所有频道 → 所有（已渲染的）消息。

    全程不需要用户操作。返回：
        {ok, teams, nChannels, nPosts, nReplies, at, partial?}

    增量模式（0.5.1，config.TEAMS_INCREMENTAL 控制，可用 incremental 覆盖）：
      · 上次完整抓取过 → 每个频道滚到「整屏都是旧消息」就停，
        没动静的频道几秒收工（原来最长 120 秒）；
      · 抓完用 merge_posts 与上次缓存合并 —— 部分抓取、
        单频道失败都不再丢历史（附件取并集）；
      · 上次是 partial（到时间上限被截断）→ 自动退回全量。
    """
    from . import teams_reader

    if deep:
        #  深度归档：参数整体放宽，且不做增量提前收工
        scroll_rounds = max(int(scroll_rounds), DEEP_SCROLL_ROUNDS)
        total_budget = max(int(total_budget), DEEP_TOTAL_BUDGET_SEC)
        incremental = False
        log.info("深度归档模式：每频道最多 %d 轮 / 单频道 %ds / 总预算 %ds",
                 scroll_rounds, DEEP_CHANNEL_BUDGET_SEC, total_budget)

    if incremental is None:
        incremental = bool(getattr(config, "TEAMS_INCREMENTAL", True))

    prev_posts: dict[str, list] = {}
    if incremental:
        prev = load()
        if prev.get("partial"):
            log.info("上次抓取被截断（partial），本次走全量")
        else:
            prev_posts = _prev_posts_by_channel(prev)
            if prev_posts:
                log.info("增量模式：%d 个频道有历史缓存", len(prev_posts))

    def say(msg: str) -> None:
        log.info(msg)
        if progress:
            try:
                progress(msg)
            except Exception:
                pass

    clear_cancel()                 # 新一轮开始，清掉上次的停止请求
    t0 = time.time()
    page = teams_reader._get_page()          # noqa: SLF001（内部函数，同包使用）
    if page is None:
        return {"ok": False, "reason": "连不上 Teams 页面",
                "teams": [], "nChannels": 0, "nPosts": 0, "nReplies": 0}

    #  ── 阶段 0：登录态体检 ──
    #  ★ 必须在枚举频道之前做 ★
    #    以前不做这一步：会话掉了之后，17 个频道会逐个报 no-pane，
    #    看起来像「频道结构读不懂」，实际是**整个 Teams 没登录**。
    #    这里先确认一次，掉了就自己弹窗恢复，恢复不了就整次失败并说清楚。
    if not _ensure_teams_ready(page, say):
        return {"ok": False,
                "reason": "需要重新登录 Teams（微软账号）：已自动打开窗口，"
                          "登录完成后会自动继续",
                "teams": [], "nChannels": 0, "nPosts": 0, "nReplies": 0,
                "need_login": True}

    #  ── 阶段 1 ──
    say("正在整理团队与频道…")
    teams = enum_teams_and_channels(page, progress=progress)
    n_ch = sum(len(t["channels"]) for t in teams)
    if not teams:
        return {"ok": False, "reason": "没读到任何团队",
                "teams": [], "nChannels": 0, "nPosts": 0, "nReplies": 0}
    say(f"共 {len(teams)} 个团队 / {n_ch} 个频道，开始读取消息…")

    #  ── 阶段 2 ──
    n_done = 0
    failed_ch: list = []          # [(team, channel, old_posts)] —— 留给补抓轮
    for t in teams:
        for ch in t["channels"]:
            if _CANCEL.is_set():
                say("已停止（已抓到的消息会保留）")
                t["_partial"] = True
                out = _finish(teams, page, partial=True)
                out["cancelled"] = True
                return out
            if time.time() - t0 > total_budget:
                say("已到时间上限，先收工，下次继续")
                t["_partial"] = True
                return _finish(teams, page, partial=True)

            n_done += 1
            #  增量抓取：把上次这个频道的消息 key 交给 read_channel，
            #  滚到「整屏都是旧消息」它就提前收工。
            old_posts = prev_posts.get(ch["id"]) or []
            known = None
            if old_posts:
                ks = {_msg_key(m) for m in old_posts if isinstance(m, dict)}
                ks = {k for k in ks if k.strip("|")}
                known = ks or None
            r = read_channel(
                page, ch["id"], scroll_rounds=scroll_rounds,
                budget_sec=(DEEP_CHANNEL_BUDGET_SEC if deep
                            else CHANNEL_BUDGET_SEC),
                known=known, deep=deep)
            if r.get("ok"):
                new_posts = r.get("posts") or []
                n_new = len(new_posts)
                #  ★ 与上次缓存合并：部分抓取不丢历史，附件取并集。
                ch["posts"] = merge_posts(new_posts, old_posts) if old_posts else new_posts
                ch["nPosts"] = len(ch["posts"])
                ch["nReplies"] = sum(int(m.get("replyCount") or 0)
                                     for m in ch["posts"])
                n_merged = len(ch["posts"]) - n_new
                if r.get("empty") and not old_posts:
                    say(f"[{n_done}/{n_ch}] {t['name']} / {ch['name']}（空）")
                elif n_merged > 0:
                    say(f"[{n_done}/{n_ch}] {t['name']} / {ch['name']}"
                        f"  新收 {n_new} 帖，合并历史后共 {ch['nPosts']} 帖"
                        f" {ch['nReplies']} 回复")
                else:
                    say(f"[{n_done}/{n_ch}] {t['name']} / {ch['name']}"
                        f"  {ch['nPosts']} 帖 {ch['nReplies']} 回复")
            else:
                #  ★ 0.5.1：读失败的频道不再清零 —— 有历史就保留（标 stale）。
                if old_posts:
                    ch["posts"] = old_posts
                    ch["nPosts"] = len(old_posts)
                    ch["nReplies"] = sum(int(m.get("replyCount") or 0)
                                         for m in old_posts)
                    ch["stale"] = True
                else:
                    ch["posts"] = []
                    ch["nPosts"] = 0
                    ch["nReplies"] = 0
                ch["error"] = r.get("reason") or "读取失败"
                failed_ch.append((t, ch, old_posts))
                say(f"[{n_done}/{n_ch}] {t['name']} / {ch['name']}"
                    f"  ✗ {ch['error']}"
                    + ("（保留上次数据，稍后补抓）" if old_posts
                       else "（稍后补抓）"))

    # ══════════ 阶段 3：补抓失败的频道 ══════════
    #  ★ 为什么必须有这一轮 ★
    #    Teams 是单页应用，抓取途中会偶发「页面空白 / 掉登录」这种**瞬时**状态。
    #    一个频道因为瞬时状态被判失败，用户就得自己再点一次按钮 ——
    #    那是把程序的问题转嫁给用户。
    #
    #    这一轮在浏览器已经热起来之后统一重试，绝大多数都能救回来。
    #    仍然失败的，才如实留在 error 里（并保留上次数据，不丢东西）。
    if failed_ch:
        say(f"补抓 {len(failed_ch)} 个失败频道…")
        saved = 0
        for t, ch, old_posts in failed_ch:
            if time.time() - t0 > total_budget + 420:
                say("补抓时间到上限，剩余频道下次再取")
                break
            r2 = read_channel(
                page, ch["id"], scroll_rounds=scroll_rounds,
                budget_sec=(DEEP_CHANNEL_BUDGET_SEC if deep
                            else CHANNEL_BUDGET_SEC),
                known=None,                    # 补抓不做增量提前收工
                deep=deep)
            if r2.get("ok"):
                new_posts = r2.get("posts") or []
                ch["posts"] = (merge_posts(new_posts, old_posts)
                               if old_posts else new_posts)
                ch["nPosts"] = len(ch["posts"])
                ch["nReplies"] = sum(int(m.get("replyCount") or 0)
                                     for m in ch["posts"])
                ch.pop("error", None)
                ch.pop("stale", None)
                saved += 1
                say(f"  补抓成功 {t['name']} / {ch['name']}"
                    f"  {ch['nPosts']} 帖 {ch['nReplies']} 回复")
            else:
                ch["error"] = r2.get("reason") or ch.get("error") or "读取失败"
                say(f"  补抓仍失败 {t['name']} / {ch['name']}：{ch['error']}")
        if saved:
            say(f"补抓完成：救回 {saved}/{len(failed_ch)} 个频道")

    return _finish(teams, page, partial=False)


def _finish(teams: list[dict], page, partial: bool) -> dict:
    """收尾：回首页 + 汇总。"""
    try:
        page.goto(HOME_URL, timeout=30000)
    except Exception:
        pass
    _guard(page)

    n_posts = sum(c.get("nPosts", 0)
                  for t in teams for c in t["channels"])
    n_replies = sum(c.get("nReplies", 0)
                    for t in teams for c in t["channels"])
    return {
        "ok": True,
        "partial": partial,
        "teams": teams,
        "nTeams": len(teams),
        "nChannels": sum(len(t["channels"]) for t in teams),
        "nPosts": n_posts,
        "nReplies": n_replies,
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


# ══════════════════════════════════════════════════════════
#  本地存储
# ══════════════════════════════════════════════════════════

CRAWL_FILE = config.DATA_DIR / "teams_crawl.json"


def save(d: dict) -> None:
    """原子写入全量抓取结果（含 EC 名单正文，丢了没法重抓）。

    ★ 以前是 `CRAWL_FILE.write_text(...)`：写一半被杀进程 / 断电就留下
      截断的 JSON，下次 load() 兜成空 → 再 save() 把这个空写回去，
      121 KB 的 Teams 历史（连同 EC 名单）就永久没了。
      现在走 store.write_json_atomic：临时文件 + .bak + os.replace。
    """
    try:
        config.ensure_dirs()
        store.write_json_atomic(CRAWL_FILE, d)
    except Exception as e:
        log.warning("存全量抓取结果失败：%s", e)


_merge_warned = False


def _count_posts(d: dict) -> int:
    """缓存里一共有多少条消息（只看已渲染的 posts，不看 nPosts 元数据）。"""
    n = 0
    for t in (d or {}).get("teams") or []:
        for c in t.get("channels") or []:
            posts = c.get("posts")
            if isinstance(posts, list):
                n += len(posts)
            else:
                n += int(c.get("nPosts") or 0)
    return n


def _merge_crawls(new: dict, old: dict) -> dict:
    """把旧的（更丰富的）缓存并回新的。

    合并规则与增量抓取一致：
      · 团队按 id（没有 id 时按名字）对齐，新的在前、旧的补在后面
      · 频道同理
      · 同一频道的消息用 merge_posts —— 新的优先，旧的补历史，附件取并集
      · 计数按合并后的实际条数重算，避免显示旧的 nPosts
    """
    def key_of(x: dict, *names: str) -> str:
        for n in names:
            v = (x.get(n) or "")
            if v:
                return str(v)
        return ""

    out = dict(new)
    n_teams = {key_of(t, "id", "name"): t for t in (new.get("teams") or [])}

    for t in (old.get("teams") or []):
        tk = key_of(t, "id", "name")
        if not tk:
            continue
        tgt_t = n_teams.get(tk)
        if tgt_t is None:
            n_teams[tk] = t
            continue
        n_chans = {key_of(c, "id", "name"): c
                   for c in (tgt_t.get("channels") or [])}
        for c in (t.get("channels") or []):
            ck = key_of(c, "id", "name")
            if not ck:
                continue
            tgt_c = n_chans.get(ck)
            if tgt_c is None:
                n_chans[ck] = c
                continue
            merged = merge_posts(tgt_c.get("posts") or [],
                                 c.get("posts") or [])
            tgt_c["posts"] = merged
            tgt_c["nPosts"] = len(merged)
            tgt_c["nReplies"] = sum(len(p.get("replies") or [])
                                    for p in merged)
        tgt_t["channels"] = list(n_chans.values())

    out["teams"] = list(n_teams.values())
    return out

def load() -> dict:
    """读取全量抓取结果。

    ★ 解析失败时**先试 .bak**，并把「损坏」这件事记进 warning。
      以前无论哪种失败都静默返回空 dict —— 调用方分不清
      「真的没有数据」和「文件坏了」，后者会被随后的 save() 固化成空。
    """
    bak = CRAWL_FILE.with_name(CRAWL_FILE.name + ".bak")
    main_d = None
    bak_d = None

    for f in (CRAWL_FILE, bak):
        if not f.exists():
            continue
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                if f is CRAWL_FILE:
                    main_d = d
                else:
                    bak_d = d
            else:
                log.warning("%s 内容不是对象，忽略", f.name)
        except Exception as e:
            log.warning("读 %s 失败：%s", f.name, e)

    if main_d is None:
        if bak_d is not None:
            log.warning("teams_crawl.json 损坏，已从 .bak 恢复读取")
            return bak_d
        return {"teams": [], "at": ""}

    #  ★ 主文件"少得多"时用 .bak 补回来 ★
    #    写入是原子的，所以文件不会坏；但部分抓取会把主文件写瘦，
    #    表现为「Teams 页突然只剩一个团队」。这里按消息条数判断，
    #    少了至少 5 条且不到旧数据的一半，就把旧历史并回来。
    if bak_d is not None:
        n_main = _count_posts(main_d)
        n_bak = _count_posts(bak_d)
        if n_bak - n_main >= 5 and n_main * 2 < n_bak:
            try:
                merged = _merge_crawls(main_d, bak_d)
                #  只提示一次：load() 每次渲染都会调，逐次刷屏没有意义
                global _merge_warned
                if not _merge_warned:
                    _merge_warned = True
                    log.warning(
                        "Teams 缓存比 .bak 少很多（%d → %d 条），"
                        "已合并历史回来（现 %d 条）",
                        n_main, n_bak, _count_posts(merged))
                return merged
            except Exception as e:
                log.warning("合并 Teams 历史失败，按原样使用：%s", e)
    return main_d


# ══════════════════════════════════════════════════════════
#  读缓存时的清洗
#
#  ★ 为什么要在「读」的时候也洗一遍：
#
#    修好抓取逻辑只对**以后新抓的**数据有用。
#    已经存在 data/teams_crawl.json 里的旧数据，
#    正文里那句「3 个赞表情符号。 3」是**当时就烤进去的** ——
#    用户在重新抓取之前，还是会看到那行莫名其妙的字。
#
#    所以读的时候顺手过一遍。成本很低（一条正则），
#    而且这样「修完立刻见效」，不需要用户手动重新抓。
#
#  ★ 只删，不改别的字 —— 正则写得尽量窄：
#      「N 个X表情符号。」  X 最多 16 个字符
#    宁可漏掉个别怪写法，也不要误伤正常句子
#    （比如学生真的在消息里写「表情符号」这个词的时候）。
# ══════════════════════════════════════════════════════════

#  ① 「N 个X表情符号。」（可选吃掉后面的裸数字）
#
#  ★ 这个正则踩过一下，值得说明白 ——
#    原文里连着两句的情况很常见：
#        '... 1 个爱心表情符号。 1 个沮丧表情符号。'
#    第一版写成 `...表情符号\s*[。.]?\s*\d*`，那个 `\d*` 是**贪心**的：
#    它把第二句开头的「1」也吞了，于是第二句只剩下
#    「个沮丧表情符号。」—— 洗不干净，反而更乱。
#
#    所以拆成两个分支，用负向先行断言卡住：
#      分支 A：后面的裸数字**如果又跟着「个」**，说明它是下一条的
#              数量，不能吃 → 这个分支不成立
#      分支 B：只吃「N 个X表情符号。」本身，不碰后面的数字
_RE_JUNK1 = re.compile(
    r"(?:\d+\s*个\s*[^\s。，,]{0,16}?表情符号\s*[。.]\s*\d+(?!\s*个)"
    r"|\d+\s*个\s*[^\s。，,]{0,16}?表情符号\s*[。.]?)")

#  ② 只剩「N 个表情符号」的简写（X 为空）
_RE_JUNK2 = re.compile(r"\d+\s*个\s*表情符号\s*[。.]?\s*\d*(?!\s*个)")

#  ③ 连数量都掉了的残句：「个沮丧表情符号。」——
#     是上面两条吃剩的，最后再兜一遍
_RE_JUNK3 = re.compile(r"个\s*[^\s。，,]{0,16}?表情符号\s*[。.]?\s*\d*")

#  ③ 从噪音里把反应**还原**出来：「3 个赞表情符号。」→ (『赞』, 3)
#     老缓存里没有 reactions 字段（那是后面才加的），
#     但这句噪音里其实带着完整信息 —— 表情名和数量都在。
#     与其删掉，不如先捞出来当反应数据用。
_RE_REAC = re.compile(r"(\d+)\s*个\s*([^\s。，,]{0,16}?)表情符号")


def _salvage_reactions(text: str) -> list[dict]:
    """从「N 个X表情符号。」里把反应捞出来。

    老缓存没有 reactions 字段，但正文里这句噪音带着
    「表情名 + 数量」两个信息。捞出来就能直接当反应显示，
    不用等用户重新抓一遍。

    认不出表情名的（表里没有）就丢掉 —— 编一个错的更糟。
    """
    if not text or "表情符号" not in text:
        return []
    out: list[dict] = []
    for m in _RE_REAC.finditer(text):
        try:
            n = int(m.group(1))
        except ValueError:
            continue
        name = (m.group(2) or "").strip()
        ch = EMO_CN.get(name)          # 只认得的才收
        if not ch:
            continue
        hit = next((x for x in out if x["emoji"] == ch), None)
        if hit:
            hit["n"] += n
        else:
            out.append({"emoji": ch, "n": n})
    return out


def _clean_junk(s: str) -> str:
    """洗掉反应区漏进正文的读屏文本。"""
    if not s or "表情符号" not in s:
        return s            # 绝大多数消息没有这个词，直接放行
    out = _RE_JUNK1.sub(" ", s)
    out = _RE_JUNK2.sub(" ", out)
    #  ③ 兜底：上面两条吃剩的残句（数量和「个」之间的东西被吃掉了）
    out = _RE_JUNK3.sub(" ", out)
    #  压掉因此产生的多余空格（换行保留，段落结构不能动）
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"[ \t]+([，,。.！!？?])", r"\1", out)
    return out.strip()


def _clean_tree(node) -> None:
    """递归清洗一棵消息树（就地改），顺手把反应捞出来。

    ★ 踩过一下：这个列表里**漏了 "teams"** ——
      于是整棵树一条都没洗到，但表面上「大部分消息是干净的」
      （因为它们本来就没噪音），很容易误判成「已经修好了」。
      实际跑一遍真缓存才发现 10 条全在。
      → 测这类清洗**必须用真实数据**，不能只看「看起来没问题」。
    """
    if isinstance(node, list):
        for x in node:
            _clean_tree(x)
        return
    if not isinstance(node, dict):
        return

    for key in ("text", "title"):
        v = node.get(key)
        if isinstance(v, str) and "表情符号" in v:
            #  先把反应捞出来（老缓存靠这一步补上 reactions 字段）
            if not node.get("reactions"):
                got = _salvage_reactions(v)
                if got:
                    node["reactions"] = got
            node[key] = _clean_junk(v)

    for key in ("replies", "posts", "channels", "teams"):
        if key in node:
            _clean_tree(node[key])


def _msg_blob(m: dict) -> str:
    """把一条消息里**所有能看见的文字**拼成一个搜索串（小写）。

    ★ 只搜 title/text/author 是不够的（实测踩过）：
      正文经常只是"见附件"，真正的内容在附件名或 PDF 里；
      讨论的关键信息又常常写在回复里。
      这些只要在本地缓存里，就应该能搜到。
    """
    parts: list[str] = [
        m.get("title") or "",
        m.get("text") or "",
        m.get("author") or "",
        m.get("file_text") or "",
        m.get("file_note") or "",
    ]
    for a in (m.get("attachments") or []):
        if isinstance(a, dict):
            parts.append(a.get("name") or "")
        elif isinstance(a, str):
            parts.append(a)
    for r in (m.get("replies") or []):
        if isinstance(r, dict):
            parts.append(r.get("author") or "")
            parts.append(r.get("text") or "")
    return " ".join(parts).lower()


def _split_terms(keyword: str) -> list[str]:
    """把搜索词拆成多个词：空格 / 逗号 / 顿号 / 分号都算分隔。

    ★ 为什么不是整串子串匹配：
      Teams 自己的搜索是「多词同时命中」——
      搜 "林里 鸭子" 期望的是两个词都在同一条消息里，
      而不是消息里真的连着写下 "林里 鸭子" 这四个字符。
    """
    import re as _re
    k = (keyword or "").strip().lower()
    if not k:
        return []
    return [x for x in _re.split(r"[\s,，、;；]+", k) if x]


def all_messages(keyword: str = "", date: str = "",
                 channel: str = "", team: str = "") -> list[dict]:
    """从本地缓存里筛消息（不联网）。

    每条消息都会补上界面需要的派生字段：
        team / channel     来源（用于分组）
        date               归一后的日期，形如 ``2026-09-02``
        timeLabel          显示用，形如 ``9/2 9:40``
        kind               ``ec`` / ``homework`` / ``post``（用于打标签）
        replyCount/replies 回复

    keyword  按标题 / 正文 / 作者模糊匹配（大小写不敏感）
    date     形如 ``2025-09-02`` / ``9/2`` / ``9月2日``
    channel  频道名包含该串
    team     团队名包含该串
    """
    d = load()
    #  读的时候顺手洗一遍 —— 老缓存里可能烤着反应区的噪音
    #  （见 _clean_junk 的说明：修抓取逻辑只管以后，读的时候洗才立刻见效）
    _clean_tree(d)
    #  用抓取时间来补年份（Teams 的时间戳不带年）
    year = _year_from(d.get("at") or "")

    kw = (keyword or "").strip().lower()
    terms = _split_terms(keyword)
    ch_f = (channel or "").strip().lower()
    tm_f = (team or "").strip().lower()
    dt = (date or "").strip()

    out: list[dict] = []
    for t in d.get("teams") or []:
        tname = t.get("name") or ""
        if tm_f and tm_f not in tname.lower():
            continue
        for c in t.get("channels") or []:
            cname = c.get("name") or ""
            if ch_f and ch_f not in cname.lower():
                continue
            for m in c.get("posts") or []:
                if terms:
                    blob = _msg_blob(m)
                    #  多词：每一个词都要出现在这条消息里（AND）
                    if not all(t in blob for t in terms):
                        continue
                if dt and not _date_hit(m.get("time") or "", dt):
                    continue

                row = dict(m)
                row["team"] = tname
                row["channel"] = cname
                row["teamId"] = t.get("id") or ""
                row["channelId"] = c.get("id") or ""
                #  派生字段
                iso = _time_to_iso(m.get("time") or "", year)
                row["date"] = iso or "未标注日期"
                row["timeLabel"] = (m.get("time") or "").strip()
                row["kind"] = _kind_of(row)      # ★ 传 row（它才有 channel）
                out.append(row)

    #  日期新的排前面（能解析出来的按解析值，解析不了的排在最后）
    out.sort(key=lambda m: _time_sort_key(m.get("time") or ""), reverse=True)
    return out


def _year_from(at: str) -> int:
    """从 ``2026-09-22 13:38:05`` 里取年份。"""
    m = re.match(r"^(\d{4})", (at or "").strip())
    return int(m.group(1)) if m else time.localtime().tm_year


def _time_to_iso(ts: str, year: int) -> str:
    """把 Teams 的时间戳归一成 ``YYYY-MM-DD``。

    能认的写法：
        ``9/2 9:40``        → 本年
        ``9/2/26 9:40``     → 指定年
        ``2026/9/2 9:40``   → 完整
        ``昨天 13:34``      → 相对今天
        ``星期五 19:44``    → 最近的那个周五
    认不出来返回空串（调用方会给「未标注日期」）。
    """
    ts = (ts or "").strip()
    if not ts:
        return ""

    m = re.match(r"^(\d{4})/(\d{1,2})/(\d{1,2})", ts)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})", ts)
    if m:
        y = int(m.group(3))
        if y < 100:
            y += 2000
        return f"{y:04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"

    m = re.match(r"^(\d{1,2})/(\d{1,2})\s", ts)
    if m:
        return f"{year:04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"

    #  相对日期
    today = date.today()
    if ts.startswith("昨天"):
        d = today - timedelta(days=1)
        return d.isoformat()
    if ts.startswith("今天"):
        return today.isoformat()

    m = re.match(r"^星期([一二三四五六日天])", ts)
    if m:
        want = "一二三四五六日天".index(m.group(1))
        #  星期一 = 0 ... 星期日 = 6
        cur = today.weekday()
        delta = (want - cur) % 7
        if delta == 0:
            delta = 7          # 说「星期X」一般是过去的那天
        return (today - timedelta(days=delta)).isoformat()

    return ""


def _kind_of(m: dict) -> str:
    """判断消息类型（给界面打标签用）。

    ec        —— 在 English Corner 频道里
    homework  —— 标题或正文里有作业 / homework / due 之类
    post      —— 其余

    注意：传进来的应该是带 `channel` 字段的整条记录，
    不是原始帖对象（原始帖没有 channel）。
    """
    blob = " ".join((
        m.get("title") or "", m.get("text") or "",
    )).lower()
    ch = (m.get("channel") or "").lower().strip()

    #  English Corner 频道：名字里带 english corner，或就叫 ec
    if "english corner" in ch or "ec " in ch or ch == "ec" \
            or ch.startswith("ec-") or ch.startswith("ec_"):
        return "ec"
    if "english corner" in blob:
        return "ec"
    if re.search(r"homework|作业|assignment|due\b|deadline", blob):
        return "homework"
    return "post"


def _date_hit(ts: str, want: str) -> bool:
    """时间戳匹配。``want`` 支持 2025-09-02 / 9/2 / 9月2日。"""
    ts = (ts or "").strip()
    if not ts:
        return False
    w = want.strip()
    #  统一成 M/D
    m = None
    if "/" in w:
        parts = w.split("/")
        if len(parts) >= 2:
            try:
                m = (int(parts[-2]), int(parts[-1]))
            except ValueError:
                m = None
    if m is None and "月" in w:
        try:
            a, b = w.replace("日", "").split("月")
            m = (int(a), int(b))
        except ValueError:
            m = None
    if m is None and "-" in w:
        parts = w.split("-")
        if len(parts) == 3:
            try:
                m = (int(parts[1]), int(parts[2]))
            except ValueError:
                m = None
    if m is None:
        return w in ts
    return f"{m[0]}/{m[1]}" in ts or f"{m[0]}/{m[1]} " in ts


def _time_sort_key(ts: str) -> tuple:
    """把各种时间写法归一成可排序的键（越新越大）。

    解析不出来的排最后（不是最前），免得脏数据污染列表头部。
    """
    import re as _re
    ts = (ts or "").strip()
    if not ts:
        return (0, 0, 0, 0)
    m = _re.match(r"^(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})", ts)
    if m:
        return (2, int(m.group(1)), int(m.group(2)),
                int(m.group(3)) * 60 + int(m.group(4)))
    m = _re.search(r"(\d{1,2}):(\d{2})", ts)
    if m:
        #  相对日期（昨天 / 星期X）没有年月，给个较低的档位
        return (1, 0, 0, int(m.group(1)) * 60 + int(m.group(2)))
    return (0, 0, 0, 0)


def stats() -> dict:
    """本地缓存的概览（给界面显示用）。"""
    d = load()
    teams = d.get("teams") or []
    n_posts = sum(c.get("nPosts", 0) for t in teams for c in t["channels"])
    n_replies = sum(c.get("nReplies", 0) for t in teams for c in t["channels"])
    return {
        "at": d.get("at") or "",
        "nTeams": len(teams),
        "nChannels": sum(len(t.get("channels") or []) for t in teams),
        "nPosts": n_posts,
        "nReplies": n_replies,
        "teams": [{
            "name": t.get("name") or "",
            "id": t.get("id") or "",
            "channels": [{
                "name": c.get("name") or "",
                "id": c.get("id") or "",
                "nPosts": c.get("nPosts", 0),
                "nReplies": c.get("nReplies", 0),
                "error": c.get("error") or "",
            } for c in (t.get("channels") or [])],
        } for t in teams],
    }
