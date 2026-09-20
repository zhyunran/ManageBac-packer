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
    import msvcrt
    _HAS_MSVCRT = True
except ImportError:      # 非 Windows 平台
    _HAS_MSVCRT = False


class ScrapeLock:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fh = None
        self._owned = False

    def acquire(self) -> bool:
        """尝试加锁；成功返回 True。"""
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
        except OSError:
            try:
                if fh is not None:
                    fh.close()
            except Exception:
                pass
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
    """with scrape_lock(path) as ok:  ok 表示是否抢到锁。"""
    lock = ScrapeLock(path)
    got = lock.acquire()
    try:
        yield got
    finally:
        lock.release()
