"""手动完成标记 —— 「我已完成，不再提示」。

【为什么需要】
    有些作业线下交了纸质版，但：
        * 老师没给成绩（graded=False）
        * 线上也没有提交徽章（submitted=False）
    于是按「唯一完成规则」永远判定为未完成，一直挂在待办里，
    看着碍眼。这类任务允许用户**手动**标记为已完成，从此不再提示。

【存储】`data/manual_done.json`
    {
      "tid:123456": {"title": "...", "course": "...", "at": "2026-09-18 12:30:00"},
      ...
    }

【键的选取】
    优先用任务 ID（URL 里的 /core_tasks/{id}）—— 最稳定，跨次抓取不变；
    没有 ID 时退回「课程 + 标题」。

【重要】手动标记**不写入** ManageBac，只存在本地 —— 不会影响学校系统。
"""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime

from . import config

log = logging.getLogger("manual_done")

FILE = config.DATA_DIR / "manual_done.json"

_lock = threading.RLock()      #  必须是可重入锁 —— 内部函数会互相调用
_cache: dict | None = None


# ---------------------------------------------------------------- 键

def key_for(url: str = "", title: str = "", course: str = "") -> str:
    """生成稳定标识。取不到任何信息时返回空串。"""
    m = re.search(r"/core_tasks/(\d+)", url or "")
    if m:
        return f"tid:{m.group(1)}"
    t = (title or "").strip()
    if t:
        return f"t:{(course or '').strip()}|{t}"
    return ""


# ---------------------------------------------------------------- 读写

def _load_nolock() -> dict:
    """内部用：**不加锁**读取（调用方必须已持锁）。"""
    global _cache
    if _cache is not None:
        return _cache
    try:
        d = json.loads(FILE.read_text(encoding="utf-8"))
        _cache = d if isinstance(d, dict) else {}
    except Exception:
        _cache = {}
    return _cache


def load() -> dict:
    """读取全部标记（带进程内缓存）。"""
    with _lock:
        return _load_nolock()


def _save_locked() -> None:
    try:
        config.ensure_dirs()
        tmp = FILE.with_suffix(f".json.tmp.{threading.get_ident()}")
        tmp.write_text(json.dumps(_cache or {}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        # Windows 上 replace 可能被短暂占用 → 重试
        import time as _t

        for i in range(5):
            try:
                tmp.replace(FILE)
                break
            except PermissionError:
                if i == 4:
                    raise
                _t.sleep(0.2 * (i + 1))
    except Exception as e:
        log.warning("保存手动标记失败：%s", e)


# ---------------------------------------------------------------- 增删查

def mark(url: str = "", title: str = "", course: str = "") -> bool:
    """标记为「我已完成，不再提示」。"""
    k = key_for(url, title, course)
    if not k:
        log.warning("无法生成标识，标记失败（缺少 URL 与标题）")
        return False
    with _lock:
        d = _load_nolock()
        d[k] = {
            "title": title or "",
            "course": course or "",
            "url": url or "",
            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        _save_locked()
    log.info("手动标记完成：%s", title or k)
    return True


def unmark(url: str = "", title: str = "", course: str = "") -> bool:
    """撤销标记（回到正常判定）。"""
    k = key_for(url, title, course)
    if not k:
        return False
    with _lock:
        d = _load_nolock()
        if k not in d:
            return False
        d.pop(k, None)
        _save_locked()
    log.info("撤销手动标记：%s", title or k)
    return True


def is_marked(url: str = "", title: str = "", course: str = "") -> bool:
    k = key_for(url, title, course)
    if not k:
        return False
    with _lock:
        return k in _load_nolock()


def count() -> int:
    with _lock:
        return len(_load_nolock())


def all_marks() -> dict:
    with _lock:
        return dict(_load_nolock())


def clear_all() -> int:
    with _lock:
        d = _load_nolock()
        n = len(d)
        d.clear()
        _save_locked()
    log.info("已清空 %d 条手动标记", n)
    return n


# ---------------------------------------------------------------- 应用

def apply(tasks) -> int:
    """把标记应用到任务列表（**就地修改**）。返回命中条数。

    * 命中 → completed=True, manual=True
    * 原本标记过但现在已撤销 → 恢复为「按提交/成绩判定」
    """
    with _lock:
        d = dict(_load_nolock())
    n = 0
    for t in tasks or []:
        k = key_for(getattr(t, "url", ""), getattr(t, "title", ""),
                    getattr(t, "course", ""))
        if k and k in d:
            t.manual = True
            t.completed = True
            n += 1
        elif getattr(t, "manual", False):
            # 撤销过 → 回到原始规则
            t.manual = False
            t.completed = bool(getattr(t, "submitted", False)
                               or getattr(t, "graded", False))
    return n
