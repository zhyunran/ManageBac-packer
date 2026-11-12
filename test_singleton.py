"""单实例守卫的离线回归测试（只测纯函数，不碰真实锁文件、不启进程）。

背景 —— 用户报告「点任务栏快捷方式打不开，点了 1 分钟没反应」
------------------------------------------------------------------
实测现场：有一个进程活着（119 MB、12 线程、持有单实例锁），
但它**没有任何可见窗口** —— 窗口已经消失，pywebview 的 GUI 循环却没返回，
所以 `webview.start()` 之后的「正常退出」永远走不到。

于是每次点图标都是：
    启动 → 单实例锁被占用 → 去把已有窗口叫到前面 → 找不到窗口 → 自己退出
界面上就是「什么都没有发生」，而且会一直持续下去。

修法：聚焦失败后先给宽限（窗口可能只是还没建出来），
仍无窗口且持锁已久 → 核对是同一程序 → 结束空转进程 → 接管并正常启动。

本测试覆盖判定用的几个纯函数。真正的「杀进程 + 接管」流程
由 `_tools` 外的脚本单独验证（会动真实锁文件，不适合放进离线套件）。
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PASS = 0
FAIL = 0


def chk(ok: bool, label: str, extra=None) -> None:
    global PASS, FAIL
    print(f"  {'[OK]' if ok else '[X] '} {label}" + ("" if ok else f"   → {extra!r}"))
    if ok:
        PASS += 1
    else:
        FAIL += 1


def main() -> int:
    print("=" * 76)
    print("  单实例守卫：判定用的纯函数")
    print("=" * 76)

    import widget

    # ── ① 同一程序判定 ──
    print("\n── ① 「是不是同一个程序」──")
    chk(widget._same_program(
        r"C:\a\ManageBac-packer.exe", r"C:\a\ManageBac-packer.exe"),
        "完整路径相同 → 是")
    chk(widget._same_program(
        r"C:\A\ManageBac-packer.EXE", r"c:\a\managebac-packer.exe"),
        "仅大小写不同 → 是（Windows 路径不区分大小写）")
    chk(widget._same_program(
        r"C:\old\ManageBac-packer.exe", r"D:\new\ManageBac-packer.exe"),
        "不同目录、同名 → 是（用户名下的两个安装位置）")
    chk(not widget._same_program(
        r"C:\Windows\explorer.exe", r"C:\a\ManageBac-packer.exe"),
        "文件名不同 → 否（防止 pid 复用误杀）")
    chk(not widget._same_program("", r"C:\a\x.exe"), "空路径 → 否")
    chk(not widget._same_program(r"C:\a\x.exe", ""), "空路径 → 否")

    # ── ② 持锁时长 ──
    print("\n── ② 持锁时长（用于区分「预热中」与「空转」）──")
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="mbpk_singleton_"))
    fake_pid_file = tmp / "app.pid"
    orig = widget._pid_path

    widget._pid_path = lambda: fake_pid_file          # type: ignore[assignment]
    try:
        chk(widget._holder_age() > 1e8,
            "pid 文件不存在时返回极大的数（宁可按最老处理）",
            widget._holder_age())

        fake_pid_file.write_text("1234\n", encoding="ascii")
        now_age = widget._holder_age()
        chk(0 <= now_age < 5, f"刚写入 → 年龄接近 0（{now_age:.1f} 秒）", now_age)

        old = time.time() - 120
        os.utime(fake_pid_file, (old, old))
        old_age = widget._holder_age()
        chk(old_age > 100, f"拨老 120 秒 → 年龄约 {old_age:.0f} 秒", old_age)
    finally:
        widget._pid_path = orig                      # type: ignore[assignment]

    # ── ③ 窗口枚举与进程映像查询的边界 ──
    print("\n── ③ 边界输入 ──")
    chk(widget._visible_windows_of(0) == [], "pid=0 → 空列表",
        widget._visible_windows_of(0))
    chk(widget._visible_windows_of(-1) == [], "pid 为负 → 空列表")
    chk(widget._image_path_of(0) == "", "pid=0 → 空字符串")
    #  一个几乎不可能存在的 pid
    chk(widget._image_path_of(999999) == "", "不存在的 pid → 空字符串",
        widget._image_path_of(999999))

    me = widget._image_path_of(os.getpid())
    chk(bool(me) and me.lower().endswith(".exe"),
        f"自身 pid 能查到可执行文件（{os.path.basename(me)}）", me)

    # ── ④ 聚焦函数在没有窗口时如实返回 False ──
    print("\n── ④ 聚焦失败要能被调用方感知 ──")
    widget._pid_path = lambda: fake_pid_file          # type: ignore[assignment]
    try:
        fake_pid_file.write_text(f"{os.getpid()}\n", encoding="ascii")
        #  本进程（测试进程）没有可见的顶层窗口
        chk(widget._focus_running_instance() is False,
            "持锁进程无可见窗口 → 返回 False（调用方据此判定是否接管）")
        fake_pid_file.write_text("0\n", encoding="ascii")
        chk(widget._focus_running_instance() is False, "pid 非法 → 返回 False")
    finally:
        widget._pid_path = orig                      # type: ignore[assignment]

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("=" * 76)
    print(f"  {PASS} 通过 / {FAIL} 失败")
    print("=" * 76)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
