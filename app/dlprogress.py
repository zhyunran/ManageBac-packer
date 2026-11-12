"""下载进度与防重入 —— 跨线程共享，界面轮询读取。

为什么需要这个模块
------------------
下载有几处会触发（任务附件、资料页单个、资料页批量），但它们**全都**是
「起一个后台线程就立刻返回」，于是：

    · 前端拿不到任何进度（只弹一个 toast 就消失）
    · 前端那句 `dataset.busy` 防护会在 API 返回的瞬间就解除 —— **形同虚设**
    · 用户以为没成功 → 再点一次 → **又起一个线程，重复下载**

实测后果：桌面「ManageBac 资料」里 `Biology.pdf` 与 `Biology (1).pdf`
**SHA256 完全相同、各 213 MB**，下载时间相差 2.5 分钟 —— 白白多下 203 MB。

所以这里要做两件事：
    1. **进度的单一来源**：所有下载都往这里写，界面只从这里读
    2. **防重入**：同一个目标路径正在下载时，第二次请求直接拒绝

为什么是「轮询」而不是回调
--------------------------
pywebview 的 js_api 调用是分线程处理的，所以界面可以在下载进行中继续调用
`download_progress()` 拿最新状态。项目里 EC 名单下载用的就是这套
（`_ec_downloading` / `_ec_progress`），行为已被验证。

速度与剩余时间怎么算
--------------------
· 速度：取最近约 1.2 秒的滑动窗口，避免瞬时抖动导致的数字乱跳
· 剩余时间：剩余字节 / 速度
· **总大小未知时不给百分比、不给剩余时间**。很多响应没有 Content-Length，
  此时只报「已下载 X · Y MB/s」。宁可少显示，也不显示一个乱跳的假倒计时。
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from pathlib import Path

log = logging.getLogger(__name__)

#  滑动窗口：只保留最近 1.2 秒的采样
_WINDOW = 1.2
_MAX_SAMPLES = 32

_LOCK = threading.RLock()

_STATE: dict = {
    "running": False,
    "name": "",
    "index": 0,          # 当前第几个（从 1 开始）
    "total": 0,          # 本次一共几个文件
    "done": 0,           # 当前文件已下载字节
    "size": 0,           # 当前文件总大小（0 = 未知）
    "started_at": 0.0,
    "finished_at": 0.0,
    "ok": None,
    "error": "",
    "skipped": 0,        # 本次因「已存在且大小一致」跳过的数量
    "failed": 0,
}

_SAMPLES: deque[tuple[float, int]] = deque(maxlen=_MAX_SAMPLES)

#  正在下载的目标路径（小写规范化）—— 防重入
_INFLIGHT: set[str] = set()


def _now() -> float:
    return time.monotonic()


def _key(path) -> str:
    try:
        return str(Path(path).resolve()).lower()
    except Exception:
        return str(path).lower()


# ---------------------------------------------------------------- 会话

def begin(total_files: int = 1) -> bool:
    """开始一次下载会话。

    返回 **True 表示由本次调用开启会话**，调用方负责在 finally 里 end()。
    返回 False 表示已在一次批量下载中（例如 download_all 里再调 download_one），
    此时**不要**调用 end()，否则会把批量的进度提前结束掉。

    这个「谁开谁关」的约定是为了支持嵌套调用：
        download_all(10 个)  → begin(10)  ← 开
            download_one()   → begin(1)   ← 返回 False，不开也不关
        download_all         → end()      ← 关
    """
    with _LOCK:
        if _STATE["running"]:
            #  已经在批量下载中：只补总数，绝不重置已有进度
            if total_files > _STATE["total"]:
                _STATE["total"] = int(total_files)
            return False
        _STATE.update(
            running=True, name="", index=0, total=max(1, int(total_files or 1)),
            done=0, size=0, started_at=_now(), finished_at=0.0,
            ok=None, error="", skipped=0, failed=0,
        )
        _SAMPLES.clear()
        _SAMPLES.append((_now(), 0))
        return True


def file_begin(name: str, size: int = 0) -> None:
    """开始下载一个文件。index 自动 +1，total 不够就补上。"""
    with _LOCK:
        if not _STATE["running"]:
            return
        _STATE["index"] += 1
        if _STATE["total"] < _STATE["index"]:
            _STATE["total"] = _STATE["index"]
        _STATE["name"] = str(name or "")
        _STATE["done"] = 0
        _STATE["size"] = max(0, int(size or 0))
        _SAMPLES.clear()
        _SAMPLES.append((_now(), 0))


def add(nbytes: int) -> None:
    """累计已下载字节，并更新速度窗口。"""
    n = int(nbytes or 0)
    if n <= 0:
        return
    with _LOCK:
        if not _STATE["running"]:
            return
        _STATE["done"] += n
        t = _now()
        _SAMPLES.append((t, _STATE["done"]))
        #  丢掉窗口外的老样本（至少留两个，否则算不出速度）
        while len(_SAMPLES) > 2 and (t - _SAMPLES[0][0]) > _WINDOW:
            _SAMPLES.popleft()


def note_skipped() -> None:
    """本文件因「已存在且大小一致」跳过。"""
    with _LOCK:
        if _STATE["running"]:
            _STATE["skipped"] += 1


def note_failed(n: int = 1) -> None:
    with _LOCK:
        if _STATE["running"]:
            _STATE["failed"] += int(n or 1)


def end(ok: bool = True, error: str = "") -> None:
    """结束一次下载会话。"""
    with _LOCK:
        _STATE.update(running=False, finished_at=_now(),
                      ok=bool(ok), error=error or "")
        _SAMPLES.clear()
        _INFLIGHT.clear()


def abort_if_idle() -> None:
    """兜底：没有任何在飞的下载却还标着 running（线程异常退出）→ 收尾。"""
    with _LOCK:
        if _STATE["running"] and not _INFLIGHT:
            elapsed = _now() - (_STATE["started_at"] or _now())
            if elapsed > 2.0:      # 给正常流程一点宽容时间
                log.warning("下载状态残留且没有在飞的请求，已自动收尾")
                _STATE.update(running=False, finished_at=_now(),
                              ok=False, error="下载线程异常结束")
                _SAMPLES.clear()


# ---------------------------------------------------------------- 防重入

def claim(path) -> bool:
    """占住一个目标路径。已被占用返回 False（说明正在下载同一个文件）。"""
    k = _key(path)
    with _LOCK:
        if k in _INFLIGHT:
            return False
        _INFLIGHT.add(k)
        return True


def release(path) -> None:
    with _LOCK:
        _INFLIGHT.discard(_key(path))


def is_claimed(path) -> bool:
    with _LOCK:
        return _key(path) in _INFLIGHT


def inflight_count() -> int:
    with _LOCK:
        return len(_INFLIGHT)


# ---------------------------------------------------------------- 读数

def _speed_locked() -> float:
    if len(_SAMPLES) < 2:
        return 0.0
    t0, b0 = _SAMPLES[0]
    t1, b1 = _SAMPLES[-1]
    dt = t1 - t0
    if dt <= 0.05:
        return 0.0
    return max(0.0, (b1 - b0) / dt)


def snapshot() -> dict:
    """给界面用的快照。所有派生字段（百分比/速度/剩余时间）都在这里算好。"""
    with _LOCK:
        st = dict(_STATE)
        speed = _speed_locked()
        done = int(st["done"])
        size = int(st["size"])

        st["speed"] = speed
        if size > 0:
            st["percent"] = max(0.0, min(100.0, done * 100.0 / size))
            remain = max(0, size - done)
            #  速度太低时算出来的剩余时间没有意义，宁可不给
            st["eta"] = (remain / speed) if speed > 512 else None
        else:
            st["percent"] = None      # 总大小未知 —— 不猜
            st["eta"] = None

        started = st["started_at"] or 0.0
        finished = st["finished_at"] or _now()
        st["elapsed"] = max(0.0, finished - started) if started else 0.0
        st["inflight"] = len(_INFLIGHT)
        return st


def text_of(st: dict | None = None) -> str:
    """一行文字版进度，方便打日志或给不显示进度条的场合用。"""
    s = st or snapshot()
    if not s.get("running"):
        return ""
    name = s.get("name") or "…"
    idx = s.get("index") or 0
    total = s.get("total") or 0
    head = f"({idx}/{total}) " if total > 1 else ""
    pct = s.get("percent")
    parts = [f"{head}{name}"]
    if pct is not None:
        parts.append(f"{pct:.0f}%")
    parts.append(f"{_human(s.get('done') or 0)}")
    sp = s.get("speed") or 0
    if sp > 0:
        parts.append(f"{_human(sp)}/s")
    eta = s.get("eta")
    if eta:
        parts.append(f"剩余 {_human_time(eta)}")
    return " · ".join(parts)


def _human(n: float) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def _human_time(sec: float) -> str:
    sec = max(0.0, float(sec or 0))
    if sec < 60:
        return f"{sec:.0f} 秒"
    if sec < 3600:
        return f"{sec//60:.0f} 分 {sec%60:.0f} 秒"
    return f"{sec//3600:.0f} 小时 {(sec%3600)//60:.0f} 分"
