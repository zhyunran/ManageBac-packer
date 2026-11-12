"""崩溃诊断：任何异常退出都留下一份可以直接贴进 GitHub issue 的报告。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【怎么保证「崩溃那一刻」日志一定写到盘上】

Python 进程出问题分三种，捕获方式完全不同，必须三管齐下：

  ① 普通未捕获异常（主线程）
     → sys.excepthook。这是最常见的：某个函数抛了没人接。

  ② 子线程里未捕获的异常
     → threading.excepthook（3.8+）。抓取线程、备线程里的异常
       在默认情况下**只打印到 stderr 就没了**，而窗口版程序没有 stderr。

  ③ 原生崩溃（访问违例 / 栈溢出 / abort / 被 DLL 带走）
     → faulthandler.enable(file=...)。这时 Python 的 excepthook
       **根本不会被调用**，进程直接没了 —— 只有 faulthandler 能
       在信号/异常处理里把各线程的调用栈写出去。
       关键：文件句柄要一直开着、且 unbuffered 写 fd，
       否则崩溃时缓冲区里的内容全丢。

  另外补一层「上一轮是否异常退出」的判断：
    启动时写一个 marker 文件，正常退出时删掉。
    下次启动如果 marker 还在 → 说明上次是被杀掉/硬崩的，
    即使什么都没写出来，至少能告诉用户「上次异常退出」，并抓现场。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【报告里有什么】

  · 时间、程序版本、exe 路径
  · 系统版本/架构、Python 版本
  · WebView2 版本、可用浏览器
  · 异常类型 + 完整 traceback（含每个栈帧的源码行）
  · 崩溃瞬间所有线程的调用栈（faulthandler 那段）
  · 最近的日志尾巴（默认 300 行）—— 这是定位问题最有用的部分
  · 环境自检（目录是否可写等）

  ★ 隐私：写之前统一过一遍 _redact()，账号密码、Cookie、JWT、
     Authorization 头、邮箱、手机号全部打码。
"""
from __future__ import annotations

import atexit
import json
import os
import platform
import re
import sys
import threading
import time
import traceback
from pathlib import Path

LOG = None                      # 由 install() 注入 logging.getLogger()
_FAULT_FILE = None              # faulthandler 的目标文件（必须长期持有）
_MARKER = None                  # 「本次运行未正常结束」标记文件
_INSTALLED = False
_STATE = {"stage": "启动中", "started": time.time()}

#  ★ faulthandler 的目标文件必须保持打开 ★
#    一旦被 GC 关掉，硬崩溃时就没有可写的地方。


def crash_dir() -> Path:
    try:
        from . import config
        d = Path(config.LOG_DIR) / "crash"
    except Exception:
        d = Path(os.environ.get("LOCALAPPDATA", ".")) / \
            "ManageBac-packer" / "logs" / "crash"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


# ────────────────────────── 打码 ──────────────────────────
_REDACT_RULES = [
    #  JSON / form 里的密码类字段
    (re.compile(r'(?i)("?(?:password|passwd|pwd|secret|token|cookie|'
                r'authorization|set-cookie)"?\s*[:=]\s*)("[^"]*"|\'[^\']*\'|[^\s,;}]+)'),
     r"\1<已隐藏>"),
    #  JWT（eyJ 开头三段）
    (re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}"),
     "<JWT 已隐藏>"),
    #  Cookie: xxx=yyy; zzz=www
    (re.compile(r"(?i)(cookie\s*:\s*)([^\r\n]{6,})"), r"\1<已隐藏>"),
    #  邮箱
    (re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"), "<邮箱>"),
    #  大陆手机号
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "<手机号>"),
]


def _redact(text: str) -> str:
    """把报告里的敏感信息替换掉。

    ★ 宁可多打码：崩溃报告是要发给别人（或贴到公开 issue）的，
      账号密码泄漏比少一条线索严重得多。
    """
    if not text:
        return ""
    out = str(text)
    for rx, rep in _REDACT_RULES:
        try:
            out = rx.sub(rep, out)
        except Exception:
            continue
    return out


# ────────────────────── 环境信息 ──────────────────────
def _env_lines() -> list:
    lines = []
    try:
        lines.append(f"程序版本：{_version()}")
        lines.append(f"Windows：{platform.system()} {platform.release()} "
                     f"(build {platform.version()})")
        lines.append(f"架构：{platform.machine()}  "
                     f"Python {platform.python_version()}")
        lines.append(f"可执行文件：{sys.executable}")
        lines.append(f"打包运行：{getattr(sys, 'frozen', False)}")
    except Exception:
        pass

    #  WebView2
    try:
        import winreg
        wv = ""
        for hive, key in (
            (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients"
             r"\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
            (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\Microsoft\EdgeUpdate\Clients"
             r"\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
            (winreg.HKEY_CURRENT_USER,
             r"SOFTWARE\Microsoft\EdgeUpdate\Clients"
             r"\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
        ):
            try:
                with winreg.OpenKey(hive, key) as h:
                    v, _ = winreg.QueryValueEx(h, "pv")
                    if v and v != "0.0.0.0":
                        wv = str(v)
                        break
            except OSError:
                continue
        lines.append(f"WebView2：{wv or '未检测到'}")
    except Exception:
        pass

    #  浏览器
    try:
        from . import browser as b
        p = b.find()
        lines.append(f"浏览器：{b.display_name(p)}（{p}）")
        lines.append("已安装：" + ("、".join(n for _k, n, _p in b.installed())
                                  or "一个都没有"))
    except Exception as e:
        lines.append(f"浏览器：检测失败（{e}）")

    #  目录
    try:
        from . import config
        for label, d in (("数据目录", config.DATA_DIR),
                         ("日志目录", config.LOG_DIR)):
            lines.append(f"{label}：{d}")
    except Exception:
        pass
    return lines


def _version() -> str:
    """版本号。

    ★ app/_version.py 里是 `VERSION = "x.y.z"` 常量（不是 __version__）★
      之前查错了属性名，报告里一直显示 unknown。
    """
    for mod in ("_version",):
        try:
            m = __import__(f"{__package__}.{mod}", fromlist=["VERSION"])
            v = getattr(m, "VERSION", "")
            if v:
                return str(v)
        except Exception:
            continue
    try:
        from . import config
        v = getattr(config, "VERSION", "")
        if v:
            return str(v)
    except Exception:
        pass
    return "unknown"


# ────────────────────── 日志尾巴 ──────────────────────
def _log_tail(n: int = 300) -> str:
    try:
        from . import config
        p = Path(config.LOG_DIR) / "widget.log"
        if not p.is_file():
            return "(没有日志文件)"
        #  从文件尾部读，避免把整个 5MB 日志读进来
        with open(p, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            back = min(size, 200_000)
            f.seek(size - back)
            data = f.read().decode("utf-8", errors="replace")
        lines = data.splitlines()[-n:]
        return "\n".join(lines)
    except Exception as e:
        return f"(读取日志失败：{e})"


# ────────────────────── 报告正文 ──────────────────────
def build_report(kind: str, exc_text: str = "",
                 fault_text: str = "", extra: dict | None = None) -> str:
    """生成一份自足的诊断报告。"""
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    parts = []
    parts.append("=" * 70)
    parts.append(" ManageBac-packer 诊断报告")
    parts.append("=" * 70)
    parts.append("")
    parts.append("【怎么用这份报告】")
    parts.append("把整个文件内容贴到 GitHub issue 里即可，不需要截图。")
    parts.append("里面已经去掉了账号、密码、Cookie 等敏感信息。")
    parts.append("")
    parts.append(f"类型：{kind}")
    parts.append(f"时间：{now}")
    parts.append(f"最后阶段：{_STATE.get('stage') or '(未知)'}")
    #  只有「本次崩溃」才写已运行时长；
    #  「上次异常结束」那份写的是当前进程的时长，纯属误导，直接不写。
    if "上次运行" not in kind:
        try:
            parts.append(f"已运行：{int(time.time() - _STATE['started'])} 秒")
        except Exception:
            pass
    parts.append("")
    parts.append("── 环境 ──")
    parts.extend(_env_lines())
    if extra:
        parts.append("")
        parts.append("── 附加信息 ──")
        for k, v in extra.items():
            parts.append(f"{k}：{v}")
    if exc_text:
        parts.append("")
        parts.append("── 异常与调用栈 ──")
        parts.append(exc_text)
    if fault_text:
        parts.append("")
        parts.append("── 崩溃瞬间的线程栈（faulthandler）──")
        parts.append(fault_text)
    parts.append("")
    parts.append("── 最近的日志 ──")
    parts.append(_log_tail())
    parts.append("")
    parts.append("=" * 70)
    parts.append(" 报告结束")
    parts.append("=" * 70)
    return _redact("\n".join(parts))


def _prune_reports(keep: int = 50) -> None:
    """只留最近若干份报告 —— 不然每次崩溃都堆一份，久了一堆垃圾。"""
    try:
        d = crash_dir()
        files = sorted(d.glob("crash-*.txt"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        for f in files[keep:]:
            try:
                f.unlink()
            except Exception:
                pass
    except Exception:
        pass


def save_report(kind: str, exc_text: str = "", fault_text: str = "",
                extra: dict | None = None) -> Path | None:
    try:
        d = crash_dir()
        _prune_reports()
        name = f"crash-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}.txt"
        p = d / name
        p.write_text(build_report(kind, exc_text, fault_text, extra),
                     encoding="utf-8")
        return p
    except Exception:
        return None


# ────────────────────── 安装钩子 ──────────────────────
def set_stage(stage: str) -> None:
    """记录「现在做到哪一步」—— 崩在这里时报告里能看出来。"""
    _STATE["stage"] = str(stage)[:200]


def install(logger=None) -> None:
    """装上三层崩溃捕获。越早调用越好（在创建窗口之前）。"""
    global LOG, _FAULT_FILE, _MARKER, _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    LOG = logger

    # ① 主线程未捕获异常
    _old_hook = sys.excepthook

    def _hook(exc_type, exc, tb):
        try:
            text = "".join(traceback.format_exception(exc_type, exc, tb))
            p = save_report("未捕获异常（主线程）", exc_text=text)
            if p and LOG:
                LOG.error("程序异常退出，诊断报告已保存：%s", p)
        except Exception:
            pass
        try:
            _old_hook(exc_type, exc, tb)
        except Exception:
            pass

    sys.excepthook = _hook

    # ② 子线程未捕获异常
    def _thread_hook(args):
        try:
            t = getattr(args, "thread", None)
            tname = getattr(t, "name", "?")
            text = "".join(traceback.format_exception(
                args.exc_type, args.exc_value, args.exc_traceback))
            p = save_report(f"未捕获异常（线程 {tname}）", exc_text=text)
            if p and LOG:
                LOG.error("线程 %s 异常，诊断报告已保存：%s", tname, p)
        except Exception:
            pass

    try:
        threading.excepthook = _thread_hook
    except Exception:
        pass

    # ③ 原生崩溃（访问违例等）
    #   ★ 文件必须一直开着，写裸 fd，崩溃时才来得及落盘 ★
    try:
        import faulthandler
        try:
            d = crash_dir()
            _FAULT_FILE = open(d / f"fault-{os.getpid()}.txt", "w",
                               encoding="utf-8", buffering=1)
            #  ★ 先写一段说明 ★
            #    faulthandler 的目标文件在启动时就必须建好并一直开着，
            #    正常运行时它一直是空的 —— 用户看到「0 字节的 fault 文件」
            #    只会以为程序生成了假日志。这里把用途写进文件本身。
            try:
                _FAULT_FILE.write(
                    "# 这个文件是「原生崩溃时的线程栈」的落盘位置（faulthandler）。\n"
                    "# 程序正常运行时，这里只有这段说明，不会有任何内容 —— "
                    "空的是正常的。\n"
                    "# 只有发生访问违例 / 栈溢出 / abort 这类原生崩溃时，"
                    "才会在下面追加各线程调用栈。\n"
                    "# 给人看的报告是同一个目录下的 crash-*.txt。\n"
                    f"# 本文件生成于 {time.strftime('%Y-%m-%d %H:%M:%S')}  "
                    f"pid={os.getpid()}\n"
                    "# " + "-" * 60 + "\n")
                _FAULT_FILE.flush()
            except Exception:
                pass
            faulthandler.enable(file=_FAULT_FILE, all_threads=True)
            #  卡死 120 秒也 dump 一次（死锁、界面无响应时很有用）
            try:
                faulthandler.dump_traceback_later(120, repeat=True,
                                                  file=_FAULT_FILE)
            except Exception:
                pass
        except Exception:
            pass
    except Exception:
        pass

    # ④ 标记文件路径（**这里不写**，交给 begin_run —— 见那里的说明）
    try:
        from . import config
        #  ★ 放 logs 而不是 data ★
        #    data\ 里的文件被升级脚本当成「用户数据」做前后计数校验；
        #    标记文件是运行状态、每次启动都可能被删，放进去会让校验
        #    误报「LOST ITEMS」（实测踩到过 157 → 156）。
        _MARKER = Path(config.LOG_DIR) / "running.marker"
    except Exception:
        _MARKER = None

    #  正常退出时删标记；异常退出时它留着，下次启动就能发现
    atexit.register(_on_clean_exit)

    if LOG:
        LOG.info("崩溃捕获已启用（报告目录：%s）", crash_dir())


def _drop_own_fault_file() -> None:
    """正常退出时删掉本进程的 fault 文件。

    ★ 它是给「崩的那一瞬间」用的 ★
      正常退出说明没崩，这个文件只剩说明头，留着只会让人困惑。
      注意：只删自己 pid 的，不碰别人的（可能还有另一个实例在跑）。
    """
    global _FAULT_FILE
    try:
        if _FAULT_FILE is not None:
            try:
                _FAULT_FILE.close()
            except Exception:
                pass
            _FAULT_FILE = None
        f = crash_dir() / f"fault-{os.getpid()}.txt"
        if f.is_file():
            f.unlink()
    except Exception:
        pass


def mark_clean() -> None:
    """标记「本次是正常退出」。

    ★ 必须显式调用一次，光靠 atexit 不够 ★
      程序里有几处是用 `os._exit()` 直接结束进程的
      （比如设置面板里的「退出程序」）—— **os._exit 不跑 atexit**，
      标记就会残留，下次启动就会误报「上次异常退出」。
    """
    try:
        if _MARKER and _MARKER.is_file():
            _MARKER.unlink()
    except Exception:
        pass
    _drop_own_fault_file()


def _on_clean_exit() -> None:
    mark_clean()


def begin_run() -> dict | None:
    """开始一次正式运行：**先读上次的标记，再写本次的标记**。

    ★ 顺序不能反 ★
      如果先写再读，读到的永远是刚写的自己，
      previous_crash() 就会恒为 True —— 每次启动都误报「上次异常退出」。
      （这个 bug 真的发生过，日志里连续两次启动都报了。）
    """
    prev = previous_crash()
    try:
        if _MARKER is not None:
            _MARKER.write_text(
                json.dumps({"pid": os.getpid(),
                            "at": time.strftime("%Y-%m-%d %H:%M:%S")},
                           ensure_ascii=False),
                encoding="utf-8")
    except Exception:
        pass
    return prev


def previous_crash() -> dict | None:
    """上次是否异常退出？返回 marker 内容。"""
    try:
        if _MARKER and _MARKER.is_file():
            try:
                d = json.loads(_MARKER.read_text(encoding="utf-8"))
            except Exception:
                d = {}
            return d if isinstance(d, dict) else {}
    except Exception:
        pass
    return None


def latest_report(since: float | None = None) -> Path | None:
    """最新的一份报告。

    since 给定时只认这个时间点**之后**生成的 ——
    「上次异常退出」提示必须用它，否则会指着一份几个月前的旧报告，
    让用户以为那份就是这次的（又一次「说的和做的不是一回事」）。
    """
    try:
        files = sorted(crash_dir().glob("crash-*.txt"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        for f in files:
            if since is None:
                return f
            try:
                if f.stat().st_mtime >= since:
                    return f
            except Exception:
                continue
        return None
    except Exception:
        return None


def report_since_marker(marker: dict | None) -> Path | None:
    """找「上次启动之后」生成的报告（没有就返回 None）。"""
    ts = None
    try:
        at = (marker or {}).get("at") or ""
        if at:
            ts = time.mktime(time.strptime(at, "%Y-%m-%d %H:%M:%S")) - 1
    except Exception:
        ts = None
    return latest_report(since=ts)


def report_previous_run(marker: dict | None = None) -> Path | None:
    """为「上次异常结束」补一份报告。

    ★ 诚实声明 ★
      进程被强制结束时拿不到调用栈，报告里必须写清这一点，
      不能用一份看起来像 traceback 的东西糊过去。
    """
    marker = marker or {}
    started = marker.get("at") or "(未知)"
    kind = "上次运行异常结束（进程被强制结束 / 断电 / 崩溃）"
    extra = {
        "上次启动时间": started,
        "上次进程号": marker.get("pid", "未知"),
        "说明": "没有拿到崩溃瞬间的调用栈：进程是被强制结束的，"
                "Python 的退出钩子没机会执行。下面这次运行开始前的日志"
                "是当时真实写下的最后记录。",
    }
    return save_report(kind, exc_text="", fault_text="", extra=extra)


def make_now(reason: str = "用户手动生成") -> Path | None:
    """不崩溃也能生成一份（排查「没崩但不对」的情况）。"""
    return save_report(reason,
                       extra={"说明": "这份是手动生成的，用于反馈问题"})
