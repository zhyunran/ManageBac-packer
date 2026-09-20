"""详情页预取 —— 让「点开卡片」变成几乎立即的。

【为什么需要】

  实测（`diag_detail_speed.py`）：
    打开一个没缓存过的任务详情 = **1.5~1.9 秒**
      · open() 装载 Cookie     1 ms   （已优化，原 1294 ms）
      · 请求详情页 HTML      1221 ms   ← 真正的瓶颈（175 KB 页面）
      · BeautifulSoup 解析    228 ms
      · 其余                    50 ms

  1.2 秒的网络请求是无法消除的（服务器就那么慢），
  但可以**提前做掉** —— 用户点之前就已经抓好了。

【策略】

  后台线程在空闲时，把「用户最可能点开的任务」的详情预先抓下来：

    ① 待办任务优先（未完成的才会去看详情）
    ② 3 天内到期的优先（最急着看）
    ③ 有附件的优先（有附件的才需要点开下载）
    ④ 一次最多预取 N 个（默认 12），避免把带宽和站点配额占满
    ⑤ 每个之间**串行 + 小间隔**，绝不用并行 —— 详情页请求较重，
       并行很容易触发站点限速（429），而限速的代价远大于预取收益
    ⑥ 冷却期内直接不预取
    ⑦ 已经有缓存的跳过（不重复抓）

  这样用户点开卡片时，95% 的情况是**读缓存**（<5 ms）。
"""
from __future__ import annotations

import logging
import threading
import time

from . import config, httpclient, taskdetail

log = logging.getLogger("prefetch")

# 一次预取多少个
#
#  为什么要抓得比较多（16 个）：
#   实测一个待办任务详情要 0.6~1.2 秒（服务器慢 + 页面 175 KB）。
#   如果只抓 5 个，用户点第 6 个还是得等 1 秒多。
#   而预取是**后台串行 + 间隔**做的，在后台进行，
#   也不会像并行那样触发站点限速。
#
#   另外「已完成的」任务不预取（用户基本不会点开看），
#   所以实际待抓数通常在 15~25 之间，16 个能覆盖大部分。
MAX_PREFETCH = 16

# 每个之间间隔多少秒（串行 + 间隔，避免触发限速）
GAP_SEC = 1.2

# 预取的总时间预算（秒）—— 超时就停，别一直占着后台
BUDGET_SEC = 150

# 是否正在预取
_running = False
_lock = threading.Lock()

# 上次预取的时间，避免频繁触发
_last_run = 0.0
MIN_INTERVAL_SEC = 10 * 60          # 10 分钟内不重复预取


def _priority(t) -> tuple:
    """排序：越靠前越该先预取。

     顺序设计：
      1. 未完成的排前面（完成的用户基本不会点开看）
      2. 3 天内的排最前（最急）
      3. 有附件的排前面（有附件的才需要展开下载）
      4. 天数越小越前
    """
    days = t.days_left if t.days_left is not None else 999
    urgent = 0 if (days is not None and -999 < days <= 3) else 1
    return (t.completed, urgent, not t.has_attachment, days)


def _is_cached(url: str) -> bool:
    tid = taskdetail._tid_of(url)
    return bool(tid and taskdetail._read_cache(tid))


def prefetch_all(tasks, max_n: int = MAX_PREFETCH,
                 gap: float = GAP_SEC,
                 budget: float = BUDGET_SEC) -> int:
    """**同步**预取（在后台线程里调用）。返回成功抓了几个。

    串行 + 间隔，遇到任何异常就停（不冒险）。
    """
    if not tasks:
        return 0

    # 冷却中不做任何请求
    if httpclient.cooldown_remaining() > 0:
        log.info("预取跳过：站点冷却中")
        return 0

    todo = [t for t in tasks if not t.completed and getattr(t, "url", "")]
    # 已有缓存的跳过
    pending = [t for t in todo if not _is_cached(t.url)]
    if not pending:
        log.info("预取跳过：所有待办都已有详情缓存")
        return 0

    pending.sort(key=_priority)
    batch = pending[:max_n]

    log.info("开始预取任务详情：%d 个（共 %d 个待确认）",
             len(batch), len(pending))

    started = time.time()
    ok_n = 0
    for i, t in enumerate(batch, 1):
        # 时间预算用完就停
        if time.time() - started > budget:
            log.info("预取到达时间预算（%.0fs），已抓 %d 个，停止",
                     budget, ok_n)
            break
        # 中途被限速就立刻停（保护账号与配额）
        if httpclient.cooldown_remaining() > 0:
            log.info("预取中途遇到限速，停止")
            break

        try:
            d = taskdetail.fetch(t.url)
            if d.get("ok"):
                ok_n += 1
                log.info("预取 [%d/%d] %s", i, len(batch),
                         (t.title or "")[:34])
            else:
                log.info("预取 [%d/%d] 失败：%s", i, len(batch),
                         d.get("error"))
        except Exception as e:
            log.warning("预取异常：%s", e)

        # 间隔（最后一个不用等）
        if i < len(batch):
            time.sleep(gap)

    log.info("预取完成：%d/%d 个（耗时 %.0fs）",
             ok_n, len(batch), time.time() - started)
    return ok_n


def start_async(tasks, force: bool = False) -> bool:
    """在后台线程里预取。返回是否真的启动了。

     会做节流（10 分钟内不重复），避免每次刷新都重新预取。
    """
    global _running, _last_run

    with _lock:
        if _running:
            return False
        if not force and (time.time() - _last_run) < MIN_INTERVAL_SEC:
            return False
        _running = True
        _last_run = time.time()

    def work():
        global _running
        try:
            prefetch_all(tasks)
        except Exception as e:
            log.warning("预取线程异常：%s", e)
        finally:
            with _lock:
                _running = False

    threading.Thread(target=work, daemon=True,
                     name="detail-prefetch").start()
    return True


def status() -> dict:
    return {
        "running": _running,
        "last_run": _last_run,
        "last_age": (time.time() - _last_run) if _last_run else None,
        "max_prefetch": MAX_PREFETCH,
        "gap_sec": GAP_SEC,
    }
