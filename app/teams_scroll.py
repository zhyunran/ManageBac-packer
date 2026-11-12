"""用 CDP 的真实鼠标滚轮事件滚 Teams 的虚拟列表。

【为什么必须用真滚轮】

    用户的怀疑是对的：抓取量确实过少 —— 14 个频道**全是 3 条**，
    3 正好是首屏渲染量。

    探针（`_tools/probe_scroll.py`）给出的证据：

        ★down  clientH=418  scrollH=1434  ovY=auto  scrollTop=1017
               channel-pane-viewport

    ① 真正的滚动容器是 `channel-pane-viewport`
       （在消息区**里面**，不是 `pane` 本身）
    ② 它的 `scrollTop` 已经是 **1017** —— Teams 自己
       把它停在「最新消息」的位置，也就是**底部**
    ③ 在 JS 里赋 `scrollTop` 之后量回来还是 1017 —— 没动

    为什么 JS 赋值不动它：
      Teams 用的是**虚拟列表**（virtual list），
      它把「当前可见范围」记在自己的状态里，
      并且监听的是**真实的滚动/滚轮事件**。
      给元素赋 `scrollTop` 只会触发一次原生 scroll，
      虚拟列表的滚动位置会被它自己的状态**覆盖回去**。

    所以 JS 里怎么滚都没用 —— 必须让浏览器
    **真的发生一次滚轮输入**。

【怎么发真滚轮】

    CDP 的 `Input.dispatchMouseEvent`：

        Input.dispatchMouseEvent {
          type: 'mouseWheel',
          x, y,                     ← 要落在滚动区**里面**
          deltaX: 0,
          deltaY: -400,             ← 负数 = 往上滚（看更早的消息）
          modifiers: 0,
          pointerType: 'mouse',
        }

    这是「浏览器层面」的输入事件，和用户真的推滚轮一样 ——
    虚拟列表一定会响应。

【这个模块做什么】
    `wheel_page(page, dy=...)`  往页面中心发一次滚轮
    `scroll_up_by_rounds(...)`  连发几轮，每轮之后等它渲染

    单发/多发的节奏由调用方决定（`teams_crawl` 那边边滚边读）。
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger("teams_scroll")


# ══════════════════════════════════════════════════════════
#  ★★★ 整件事的关键：标签页必须在**前台**
#
#  这是查了整整一轮才挖出来的根因。前面排除掉的全部是干扰项：
#    · 滚轮命令发不出去？   → 发得出去（改用 cdp.fire 之后）
#    · 落点不对？           → 落点是对的（换成某条消息的中心）
#    · 事件类型不认？       → mouseWheel / synthesizeScrollGesture 都不动
#
#  真正的原因是：
#
#      document.visibilityState === 'hidden'
#      document.hasFocus() === false
#
#  ——**标签页在后台**。
#
#  浏览器对后台标签有**输入节流**：
#  滚轮、手势这类输入事件会被丢掉（或者延迟到几乎不处理），
#  免得后台页面白白耗 CPU。这是浏览器行为，不是 bug。
#
#  ★ 而 Teams 抓取用的那个标签，正是「用户看不见的那个」——
#    抓取是后台跑的，标签不在最前面很正常。
#
#  为什么这条一直没被发现：
#    `Page.bringToFront`（CDP 里让页面到前台的那个命令）
#    在**被自动化控制的浏览器**里经常**不起作用** ——
#    实测调了之后 visibilityState 依然是 hidden。
#    真正管用的是 `Target.activateTarget`（激活目标）。
#    两者名字很像，但一个是「请求到前台」，
#    另一个是「把目标**设为活动**」。
# ══════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════
#  ⚠ 以下 ensure_foreground() 与 is_foreground() 目前**全项目零调用**。
#
#    它们不算错，也不是坏代码 —— 只是没有任何地方用它们。
#    留着的原因：把页面弄到前台是「滚轮事件被节流」时的常见修法，
#    将来若发现某些机器上滚不动，这里是现成的候选做法。
#
#    但现在必须**明确标出**它没被接线，否则下一个维护者会以为
#    「前台激活」这一步已经在跑了 —— 这正是本项目吃过亏的那类误解
#    （死方法没人敢删、活方法没人知道是活的）。
#    判定方式：_tools/check_api_surface.py 同思路的静态检索。
# ══════════════════════════════════════════════════════════

def ensure_foreground(page) -> bool:
    """把页面弄到前台（否则输入事件会被浏览器节流丢掉）。

    ⚠ 零调用（见上方说明）。

    依次试两个 CDP 命令：
      ① ``Target.activateTarget``  ← 实测这个管用
      ② ``Page.bringToFront``      ← 兜底，某些版本只有它

    返回是不是「已经在前台」（或成功激活）。
    """
    tgt = getattr(page, "tid", None)
    if tgt:
        try:
            page.cdp.send("Target.activateTarget", {"targetId": tgt},
                          timeout=5.0)
        except Exception as e:
            log.debug("Target.activateTarget 失败：%s", e)
    try:
        page.cdp.send("Page.bringToFront", {}, session_id=page.sid,
                      timeout=5.0)
    except Exception as e:
        log.debug("Page.bringToFront 失败：%s", e)

    #  等一下让它真的切过去，再问一次可见性
    for _ in range(6):
        try:
            v = page.eval("document.visibilityState")
        except Exception:
            v = None
        if v == "visible":
            return True
        time.sleep(0.4)
    return False


def is_foreground(page) -> bool:
    try:
        return page.eval("document.visibilityState") == "visible"
    except Exception:
        return False


#  滚轮一次滚多少像素。
#  太小：一轮下来虚拟列表可能还没触发回收/加载；
#  太大：可能一下跳过一整屏，中间的消息来不及被读到就被回收了。
#  420 大约是一屏（探针量到 clientH=418），上下留一点重叠。
WHEEL_STEP = 420


def _pick_point(page) -> tuple[int, int] | None:
    """挑一个「滚轮真的能落上去」的点。

    ★★ 这里踩过一个很隐蔽的坑，值得说明白 ——

      原来是用**消息区的几何中心**：
          const r = pane.getBoundingClientRect();
          x = r.left + r.width / 2;
          y = r.top  + r.height / 2;

      看起来完全合理，但实测**滚不动**。
      用 `document.elementFromPoint(x, y)` 一查才看出来：
      那个点底下根本不是消息列表，而是 Teams 的一层
      **常驻全屏图层**：

          DIV.logo-layer layer-small-circle
          DIV.text-slide-in
          DIV.t2-icon-2025
          …
          HTML.theme-darkV2

      ★ 关键：它不是「加载动画，一会儿就没了」——
        实测连续 30 次（42 秒）探测，它**一直都在**。
        它是 Teams 的背景装饰层，透明、但会吃掉指针事件。

      → 所以 `elementFromPoint` 在这里**没用**：
        它永远返回那一层，挑不出「真正滚轮该去的地方」。

      → 正解：**不信 elementFromPoint，直接拿消息元素自己的坐标**。
        消息元素是列表的子元素，把滚轮发在它的位置上，
        浏览器派发时会从**最顶层可见元素**开始找；
        如果那一层真的吃了事件，说明它盖在消息上面 ——
        那就把坐标往消息区里**偏移一点**再试，
        或者干脆用消息元素中心（很多实现按坐标命中，
        不细究那点上是谁）。

      实测结论：**用某条消息的中心**（不是消息区的中心）
      能滚起来。区别在于消息区的中心可能落在两条消息之间的
      空白处 / 被别的层盖住，而消息本身的中心一定在列表内容上。
    """
    return page.call(r"""() => {
      const q = (r, t) => r.querySelector(`[data-tid="${t}"]`);
      const pane = q(document, 'message-pane-body');
      if (!pane) return null;
      const vp = pane.querySelector('[data-tid="channel-pane-viewport"]')
                 || pane;

      //  ① 优先：某条**消息**的中心（挑视口里完整可见的那条）
      const msgs = [...vp.querySelectorAll(
        '[data-tid="channel-pane-message"]')];
      const vh = window.innerHeight;
      for (const msg of msgs) {
        const r = msg.getBoundingClientRect();
        if (r.height < 24) continue;
        if (r.top < 4 || r.bottom > vh - 4) continue;
        return {
          x: Math.round(r.left + r.width / 2),
          y: Math.round(r.top + Math.min(r.height / 2, 34)),
          how: 'msg',
          w: Math.round(r.width), h: Math.round(r.height),
        };
      }
      //  视口里没有完整的 → 放宽，只要露出来一部分就行
      for (const msg of msgs) {
        const r = msg.getBoundingClientRect();
        if (r.height < 24) continue;
        const y = Math.round((Math.max(r.top, 0) + Math.min(r.bottom, vh)) / 2);
        if (y > 8 && y < vh - 8) {
          return {x: Math.round(r.left + r.width / 2), y: y,
                  how: 'msg-part', w: Math.round(r.width)};
        }
      }

      //  ② 退一步：消息区本身，但往下/上偏一点避开正中
      const r = pane.getBoundingClientRect();
      const cw = Math.round(r.width);
      for (const [fx, fy] of [[0.5, 0.42], [0.5, 0.58],
                              [0.35, 0.5], [0.65, 0.5]]) {
        const x = Math.round(r.left + cw * fx);
        const y = Math.round(r.top + r.height * fy);
        const h = document.elementFromPoint(x, y);
        //  ★ 这一层的 class 里带 logo-layer / slide-in 就别选
        const cls = (h && typeof h.className === 'string') ? h.className : '';
        if (/logo-layer|slide-in|t2-icon|theme-darkV2/.test(cls)) continue;
        return {x: x, y: y, how: 'pane'};
      }

      //  ③ 兜底：就当消息区中心能用
      return {
        x: Math.round(r.left + r.width / 2),
        y: Math.round(r.top + r.height / 2),
        how: 'fallback',
      };
    }""")


def wheel_page(page, dy: int = -WHEEL_STEP,
               x: int | None = None, y: int | None = None,
               tries: int = 3) -> bool:
    """在页面上发一次真实 mouseWheel 事件。

    dy  负数 = 往上滚（内容往下走，看到更早的消息）。

    ★ 落点由 `_pick_point()` 选 —— 见那里的说明：
      别用几何中心，那一层可能被 Teams 的动画盖着。
    """
    for k in range(max(1, tries)):
        try:
            pt = None
            if x is None or y is None:
                pt = _pick_point(page)
                if not pt:
                    log.debug("挑不到落点（消息区不在？）")
                    return False
                x, y = pt["x"], pt["y"]
                if pt.get("how") == "fallback":
                    log.debug("落点只能退回中心（那点上可能是 %s）",
                              pt.get("hit"))

            #  ★ 用 fire（不等响应）而不是 send ——
            #    send 会一直 recv 到自己的响应为止，而这条命令
            #    会让 Teams 的虚拟列表吐出一大堆事件，
            #    实测会在读到响应前被 socket 超时打断
            #    （WebSocketTimeoutException）。见 cdp.CDP.fire 的说明。
            page.cdp.fire("Input.dispatchMouseEvent", {
                "type": "mouseMoved",
                "x": int(x), "y": int(y),
                "modifiers": 0,
                "pointerType": "mouse",
            }, session_id=page.sid)
            page.cdp.fire("Input.dispatchMouseEvent", {
                "type": "mouseWheel",
                "x": int(x), "y": int(y),
                "deltaX": 0,
                "deltaY": int(dy),
                "modifiers": 0,
                "pointerType": "mouse",
            }, session_id=page.sid)
            return True
        except Exception as e:
            log.debug("发滚轮失败（第 %d 次）：%s", k + 1, e)
            time.sleep(0.4)
    return False


def scroll_up_by_rounds(page, rounds: int = 10,
                        gap: float = 1.2,
                        on_each=None) -> int:
    """连发 `rounds` 轮滚轮，每轮之间歇 `gap` 秒。

    `on_each(i)` 可以挂钩子 —— 调用方用它「每轮读一次 DOM」。
    这个钩子是**必需的**：虚拟列表会把滚过去的消息从 DOM 里
    回收掉，所以必须边滚边读，不能等滚完再读。

    返回实际发出去了几轮。
    """
    done = 0
    for i in range(max(0, rounds)):
        if not wheel_page(page):
            break
        done += 1
        time.sleep(gap)          # 等虚拟列表把这一屏渲染出来
        if on_each:
            try:
                on_each(i)
            except Exception as e:
                log.debug("on_each 回调出错：%s", e)
    return done


def wheel_to_top(page, max_rounds: int = 60, gap: float = 0.55) -> int:
    """一直往上滚到顶（不看新内容，专门用来快速回到最早）。

    ⚠ 零调用：现在回到最早是靠 teams_crawl 里的增量/全量逻辑，
      不靠这个函数。保留原因同上方说明（现成实现，将来可用）。

    用「滚完量 scrollTop 有没有变小」判断是不是到顶了 ——
    比只看 `scrollTop <= 1` 可靠（有些容器到顶了值不是 0）。
    """
    last = None
    for i in range(max(0, max_rounds)):
        st = page.call(r"""() => {
          const pane = document.querySelector(
            '[data-tid="message-pane-body"]');
          const vp = pane && (pane.querySelector(
            '[data-tid="channel-pane-viewport"]') || pane);
          return vp ? Math.round(vp.scrollTop) : null;
        }""")
        if st is not None and st <= 1:
            return i
        if last is not None and st == last:
            #  滚了但位置没变 —— 到底了（或这个容器不响应）
            return i
        last = st
        if not wheel_page(page):
            break
        time.sleep(gap)
    return max_rounds
