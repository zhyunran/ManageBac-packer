"""单实例锁 —— 避免多个进程同时驱动同一个 Edge 实例。

场景：桌面小组件与网页版看板可能同时开着。
两者都要抓数据，但 Edge 的数据目录是同一个，同时启动会互相干扰。
用文件锁保证「同一时刻只有一个进程在抓取」，另一个只读缓存。
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

try:
    import logging
    log = logging.getLogger("mb.lock")
except Exception:                                    # pragma: no cover
    log = None

try:
    import msvcrt
    _HAS_MSVCRT = True
except ImportError:      # 非 Windows 平台
    _HAS_MSVCRT = False


#  Windows 上「锁被别的进程占着」对应的 errno。
#    msvcrt.locking(LK_NBLCK) 抢不到时给 EACCES（部分情况 EDEADLK）。
#  ★ 其余 OSError（权限不够、盘满、被安全软件拦下……）**不是**竞争，
#    是真故障 —— 必须能区分开，否则会被当成「另一个进程在抓」。
_CONTENDED_ERRNOS = {13, 36}          # EACCES, EDEADLK


def _is_contention(e: OSError) -> bool:
    """这个 OSError 是「别人占着锁」，还是「真的出错了」？"""
    if getattr(e, "winerror", None) == 33:      # ERROR_LOCK_VIOLATION
        return True
    return getattr(e, "errno", None) in _CONTENDED_ERRNOS


class ScrapeLock:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fh = None
        self._owned = False
        #  ★ 抢锁失败时这里会写明原因：
        #      ""       = 不是失败，或只是别人占着（正常竞争）
        #      非空字符串 = **真故障**（权限 / I/O / 安全软件），调用方必须如实上报
        self.last_error: str = ""

    @property
    def acquired(self) -> bool:
        """是否已持有锁（对外只读，替代内部的 _owned）。"""
        return self._owned

    def acquire(self) -> bool:
        """尝试加锁；成功返回 True。

        失败时 self.last_error 说明是「竞争」还是「真故障」。
        """
        if self._owned:
            return True

        fh = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(self.path, "a+b")
            if fh.seek(0, os.SEEK_END) == 0:
                fh.write(b"0")
                fh.flush()
            fh.seek(0)
            if _HAS_MSVCRT:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            self._fh = fh
            self._owned = True
            try:
                fh.seek(0)
                fh.truncate()
                fh.write(str(os.getpid()).encode())
                fh.flush()
                fh.seek(0)
            except Exception:
                pass
            return True
        except OSError as e:
            try:
                if fh is not None:
                    fh.close()
            except Exception:
                pass
            if _is_contention(e):
                #  正常竞争：别的进程在抓，静默返回 False 即可
                self.last_error = ""
            else:
                #  真故障：写清楚，让上层不要把它说成「另一窗口正在抓取」
                self.last_error = f"{type(e).__name__}: {e}"
                if log:
                    log.warning("抓取锁不可用（不是竞争，是真故障）：%s", e)
            return False

    def release(self) -> None:
        if not self._owned or self._fh is None:
            return
        try:
            if _HAS_MSVCRT:
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
        except Exception:
            pass
        try:
            self._fh.close()
        except Exception:
            pass
        self._fh = None
        self._owned = False

    # 便于 with 使用：with 块内通过 `as got` 拿到是否成功
    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()


@contextmanager
def scrape_lock(path: Path):
    """with scrape_lock(path) as ok:  ok 表示是否抢到锁。

    注意：这只告诉你「抢到没有」。要区分「别人占着」与「锁坏了」，
    用 scrape_lock_ex()。
    """
    lock = ScrapeLock(path)
    got = lock.acquire()
    try:
        yield got
    finally:
        lock.release()


@contextmanager
def scrape_lock_ex(path: Path):
    """同 scrape_lock，但把锁对象交出来：`as lk` 后可读 lk.last_error。

    给需要「如实报告失败原因」的调用方用（见 pipeline._run_refresh）：
    抢不到锁有两种性质完全不同的原因 ——
      · 别人正在抓        → 正常竞争，读缓存是对的
      · 锁文件开不了/写不了 → 真故障，说成「另一窗口正在抓取」就是撒谎
    """
    lock = ScrapeLock(path)
    lock.acquire()                      # 失败也不抛，看 lock.owned
    try:
        yield lock
    finally:
        lock.release()
