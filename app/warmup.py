"""预热 —— 让小组件「打开即有内容」。

设计目标（用户要求）：
    以后打开即有内容；或者开机启动，自动抓取，免得开启后等待。

三条路径：
    1. **打开即有内容**：`widget.py` 启动时先读缓存渲染 → 界面秒开
    2. **自动补数据**：缓存过期 / 未登录时，后台静默登录 + 抓取
    3. **开机预热**：登录 Windows 时后台跑一次，等用户打开时数据已就绪

 安全铁律（踩过大坑）
    账号因「连续 5 次登录失败」会被**临时锁定**。
    因此本模块**绝不盲目重试**：识别出 locked / 密码错误就立即停止，
    并把原因写进状态文件，让界面能明确告诉用户。
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path

from . import config, cdp, httpclient, store

log = logging.getLogger("warmup")

STATUS_FILE = config.DATA_DIR / "warmup.json"

# 缓存多久算「过期」（秒）。开机预热后 30 分钟内打开就不必再抓。
STALE_SEC = 30 * 60


# ---------------------------------------------------------------- 状态

def read_status() -> dict:
    """读取上次预热结果（供界面显示）。"""
    try:
        return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_status(**kw) -> dict:
    """写入预热状态。"""
    data = read_status()
    data.update(kw)
    data["at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data["at_ts"] = time.time()
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATUS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(STATUS_FILE)
    except Exception as e:
        log.warning("写入预热状态失败：%s", e)
    return data


def cache_age() -> float:
    """缓存文件距今多少秒。没有缓存时返回一个很大的数。"""
    try:
        return max(0.0, time.time() - config.CACHE_FILE.stat().st_mtime)
    except Exception:
        return 1e9


def cache_is_fresh(max_age: float = STALE_SEC) -> bool:
    return cache_age() < max_age


# ---------------------------------------------------------------- 主流程

def startup_prepare(verbose: bool = False) -> dict:
    """小组件启动时调用：**只抓一次**，且遵守限速冷却。

    与 `ensure_fresh` 的区别：
        * 冷却期内直接返回（不发任何请求）
        * 有缓存就先刷新界面，再判断是否需要抓新数据
        * 绝不重复抓取（避免抢锁 / 双重请求触发限速）
    """
    def say(msg: str) -> None:
        if verbose:
            print(f"  {msg}", flush=True)

    config.ensure_dirs()

    # ---- 1. 冷却中？直接返回，什么都不做 ----
    cd = httpclient.cooldown_remaining()
    if cd > 0:
        say(f"站点冷却中（还剩 {int(cd)} 秒），跳过抓取")
        return write_status(ok=True, skipped=True, reason="cooldown",
                            message=f"站点冷却中，还剩 {int(cd)} 秒")

    # ---- 2. 缓存够新？直接返回 ----
    if cache_is_fresh(10 * 60):
        age = int(cache_age())
        say(f"缓存 {age // 60} 分钟前更新，够新，跳过抓取")
        return write_status(ok=True, skipped=True, reason="cache_fresh",
                            message=f"数据是 {age // 60} 分钟前的")

    # ---- 3. 抓一次（含自动登录） ----
    return ensure_fresh(verbose=verbose, force=True)


def ensure_fresh(verbose: bool = True, force: bool = False,
                 max_age: float = STALE_SEC) -> dict:
    """确保数据是新的。返回状态 dict。

    流程：
        1. 缓存够新 → 直接返回（不碰浏览器，秒回）
        2. 已登录    → 直接抓取
        3. 未登录    → 用凭据自动登录一次（**不重试**），再抓取

    参数：
        force   : 忽略缓存新鲜度，强制抓一次
        max_age : 缓存可接受的年龄（秒）
    """
    def say(msg: str) -> None:
        if verbose:
            print(f"  {msg}", flush=True)

    config.ensure_dirs()

    # 0) 冷却期内不折腾（避免把「限速」拖成「看起来卡死」）
    cd = httpclient.cooldown_remaining()
    if cd > 0:
        say(f"站点冷却中（还剩 {int(cd)} 秒），本次跳过")
        return write_status(ok=True, skipped=True, reason="cooldown",
                            message=f"站点冷却中，还剩 {int(cd)} 秒")

    # 1) 缓存够新就不折腾
    if not force and cache_is_fresh(max_age):
        age = int(cache_age())
        say(f"缓存还很新（{age // 60} 分钟前），无需抓取")
        return write_status(ok=True, skipped=True, reason="cache_fresh",
                            message=f"数据是 {age // 60} 分钟前的，无需刷新")

    # 2) 检查登录态（只读探测，绝不提交表单）
    say("检查登录状态……")
    logged_in = probe_login()
    login_error = ""

    if not logged_in:
        say("未登录，尝试用保存的凭据自动登录……")
        ok, msg = _auto_login()
        if ok:
            logged_in = True
            say(f"登录成功：{msg}")
        else:
            login_error = msg
            say(f"自动登录未成功：{msg}")
            #  被锁定 / 密码错误 → 立即返回，绝不重试
            if any(k in msg for k in ("锁定", "锁", "不正确")):
                return write_status(ok=False, status="need_login",
                    reason="locked" if "锁" in msg else "bad_credentials",
                    message=msg,
                )
    else:
        say("已是登录状态")

    if not logged_in:
        return write_status(ok=False, status="need_login", reason="not_logged_in",
                            message=login_error or "还没有登录，请先填一次账号")

    # 3) 抓取
    say("正在抓取数据……")
    try:
        from . import pipeline

        pipeline.load_from_cache()
        pipeline.refresh_sync()
        snap = pipeline.snapshot()
    except Exception as e:
        log.exception("抓取失败")
        return write_status(ok=False, status="error", reason="fetch_failed",
                            message=f"抓取失败：{type(e).__name__}: {e}")

    st = snap.get("status")
    msg = snap.get("message") or ""
    n_tasks = len(snap.get("tasks") or [])
    n_courses = len(snap.get("courses") or [])
    n_lessons = len((snap.get("schedule") or {}).get("lessons") or [])

    if st in ("ok", None) and (n_tasks or n_courses):
        say(f"完成：{n_tasks} 个任务 / {n_courses} 门课程 / 课表 {n_lessons} 节")
        return write_status(ok=True, status="ok", reason="fetched", message=msg,
            tasks=n_tasks, courses=n_courses, lessons=n_lessons,
        )

    if st == "need_login":
        say("抓取时发现登录态失效")
        return write_status(ok=False, status="need_login", reason="session_expired",
                            message="登录状态已过期，请重新登录一次")

    say(f"抓取未成功：{msg}")
    return write_status(ok=False, status=st or "error", reason="fetch_incomplete",
                        message=msg or "抓取未完成")


def probe_login() -> bool:
    """只读探测登录态。不提交任何表单，不会触发锁定。

     只走纯 HTTP —— **绝不启动浏览器**。
      早期版本在 HTTP 探测失败时会退回浏览器（启动无头 Edge），
      副作用太大：用户双击图标后会莫名看到 Edge 进程，
      而且启动浏览器要 5~10 秒。而我们只是想「问一句还有效吗」。

      现在的判断：
        · 429（限速）  → 保守认为「还有效」（重登会更糟）
        · 302 → /login → 真的失效
        · 200          → 有效
        · 其他异常     → 保守认为「有效」，别贸然重登
    """
    try:
        if not httpclient.has_saved_session():
            return False

        s = httpclient.HttpSession(auto_login_on_demand=False)
        try:
            s.open()
            return bool(s.logged_in)
        finally:
            s.close()
    except Exception as e:
        log.info("HTTP 登录探测异常（保守认为有效）：%s", e)
        return True


def _auto_login() -> tuple[bool, str]:
    """调用 auto_login 完成一次登录（内部已保证不重试）。"""
    try:
        import auto_login as al

        return al.do_login(verbose=False, headless=True)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------- 入口

def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="预热：自动登录并抓取，让小组件打开即有内容")
    ap.add_argument("--force", action="store_true", help="忽略缓存，强制抓取")
    ap.add_argument("--max-age", type=float, default=STALE_SEC,
                    help=f"缓存可接受的年龄（秒），默认 {STALE_SEC}")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.FileHandler(config.LOG_DIR / "warmup.log",
                                     encoding="utf-8")],
    )
    config.ensure_dirs()

    verbose = not args.quiet
    if verbose:
        print("=" * 68)
        print("  ManageBac 预热")
        print("=" * 68)

    st = ensure_fresh(verbose=verbose, force=args.force, max_age=args.max_age)

    if verbose:
        print()
        print(f"  结果：{'[OK]' if st.get('ok') else '[X] '} {st.get('message', '')}")
        if st.get("reason") == "locked":
            print("  ┌──────────────────────────────────────────────────────┐")
            print("  │  账号被临时锁定 —— 请等待 15~30 分钟再试。            │")
            print("  │  期间不要反复登录，否则会重置锁定计时器。             │")
            print("  └──────────────────────────────────────────────────────┘")
    return 0 if st.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
