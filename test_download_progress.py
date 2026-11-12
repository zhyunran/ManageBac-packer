"""下载进度的回归测试（离线，不发网络请求）。

背景
----
所有下载接口都是「起后台线程立刻返回」，界面拿不到过程信息，于是用户以为
没成功就再点一次 —— 实测桌面里出现 `Biology.pdf` 与 `Biology (1).pdf`，
**SHA256 完全相同、各 213 MB**，白白重复下载 203 MB。

这里钉住四件事：
  ① 进度会话的生命周期（begin → file_begin → add → end）
  ② 百分比/速度/剩余时间的推导，以及**总大小未知时不给百分比**
  ③ 防重入：同一个目标路径不能被同时占两次
  ④ 嵌套 begin 的语义：批量里再调单个，**不能**把批量的进度提前结束
"""
from __future__ import annotations

import pathlib
import sys
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
    print("  下载进度与防重入")
    print("=" * 76)

    from app import dlprogress as dp

    # ── ① 生命周期 ──
    print("\n── ① 进度会话生命周期 ──")
    chk(dp.snapshot()["running"] is False, "初始状态不在下载")

    owner = dp.begin(3)
    chk(owner is True, "begin() 返回 True（本次开启会话）", owner)
    st = dp.snapshot()
    chk(st["running"] is True, "开始后 running=True")
    chk(st["total"] == 3, "总数 = 3", st["total"])

    dp.file_begin("Chapter_2_project.pdf", size=1000)
    st = dp.snapshot()
    chk(st["index"] == 1, "第一个文件 index=1", st["index"])
    chk(st["name"] == "Chapter_2_project.pdf", "文件名已记录", st["name"])
    chk(st["size"] == 1000, "总大小 1000", st["size"])
    chk(st["percent"] == 0.0, "刚开始 0%", st["percent"])

    dp.add(250)
    chk(dp.snapshot()["percent"] == 25.0, "下 250 → 25%", dp.snapshot()["percent"])
    dp.add(250)
    chk(dp.snapshot()["percent"] == 50.0, "再下 250 → 50%", dp.snapshot()["percent"])
    chk(dp.snapshot()["done"] == 500, "已下载 500", dp.snapshot()["done"])

    dp.file_begin("第二个.pdf", size=0)
    st = dp.snapshot()
    chk(st["index"] == 2, "第二个文件 index=2", st["index"])
    chk(st["done"] == 0, "新文件已下载量归零", st["done"])

    dp.end()
    chk(dp.snapshot()["running"] is False, "end() 后 running=False")

    # ── ② 总大小未知时不给百分比/剩余时间 ──
    print("\n── ② 总大小未知（没有 Content-Length）──")
    dp.begin(1)
    dp.file_begin("stream.bin", size=0)
    dp.add(80 * 1024)
    st = dp.snapshot()
    chk(st["percent"] is None, "percent 为 None（不猜）", st["percent"])
    chk(st["eta"] is None, "eta 为 None（不给假倒计时）", st["eta"])
    chk(st["done"] == 80 * 1024, "已下载量仍然准确", st["done"])
    dp.end()

    # ── ③ 速度与剩余时间 ──
    print("\n── ③ 速度 / 剩余时间 ──")
    dp.begin(1)
    dp.file_begin("big.pdf", size=2 * 1024 * 1024)
    #  模拟 1 MB/s：分 4 次、每 250ms 各 256KB
    for _ in range(4):
        time.sleep(0.25)
        dp.add(256 * 1024)
    st = dp.snapshot()
    speed = st["speed"]
    chk(speed > 100 * 1024, f"测到速度 {speed/1024:.0f} KB/s（应 >100KB/s）", speed)
    chk(st["eta"] is not None, "算出剩余时间", st["eta"])
    if speed > 0:
        expect = (2 * 1024 * 1024 - st["done"]) / speed
        chk(abs(st["eta"] - expect) < 1.0,
            f"剩余时间 = 剩余字节 / 速度（{st['eta']:.1f}s vs {expect:.1f}s）",
            (st["eta"], expect))
    dp.end()
    chk(dp.snapshot()["speed"] == 0.0, "结束后速度归零")

    # ── ④ 防重入 ──
    print("\n── ④ 防重入（同路径不能同时占两次）──")
    target = pathlib.Path(r"C:\tmp\dup_test.pdf")
    chk(dp.claim(target) is True, "第一次占位成功")
    chk(dp.claim(target) is False, "第二次占位被拒（同路径正在下载）")
    chk(dp.inflight_count() == 1, "在飞计数 = 1", dp.inflight_count())
    dp.release(target)
    chk(dp.claim(target) is True, "释放后可以再占")
    dp.release(target)
    #  大小写与相对/绝对路径应视为同一目标
    chk(dp.claim(pathlib.Path(r"C:\TMP\DUP_test.PDF")) is True, "占住大写版本")
    chk(dp.claim(target) is False, "小写同路径也被识别为重复（Windows 不区分大小写）")
    dp.release(pathlib.Path(r"C:\TMP\DUP_test.PDF"))
    dp.begin(1)
    dp.end()
    chk(dp.inflight_count() == 0, "end() 清空在飞集合", dp.inflight_count())

    # ── ⑤ 嵌套 begin：批量里调单个，不能提前结束批量 ──
    print("\n── ⑤ 嵌套 begin 的语义 ──")
    outer = dp.begin(5)
    chk(outer is True, "批量 begin(5) 开启会话")
    inner = dp.begin(1)
    chk(inner is False, "批量内部 begin(1) 返回 False（不接管）", inner)
    chk(dp.snapshot()["total"] == 5,
        "总数仍是 5（没被内层改成 1）", dp.snapshot()["total"])
    chk(dp.snapshot()["running"] is True, "仍处于下载中")
    dp.end()
    chk(dp.snapshot()["running"] is False, "结束")

    # ── ⑥ 跳过计数 ──
    print("\n── ⑥ 「已存在且大小一致」的跳过计数 ──")
    dp.begin(2)
    dp.file_begin("a.pdf", size=100)
    dp.note_skipped()
    dp.file_begin("b.pdf", size=100)
    st = dp.snapshot()
    chk(st["skipped"] == 1, "skipped = 1", st["skipped"])
    dp.end()

    # ── ⑦ 一行文字版（日志/无进度条场合用）──
    print("\n── ⑦ 文字版进度 ──")
    dp.begin(8)
    dp.file_begin("x.pdf", size=1000)
    dp.add(500)
    txt = dp.text_of()
    chk("(1/8)" in txt, f"含批次号 (1/8)：{txt!r}", txt)
    chk("50%" in txt, f"含百分比 50%：{txt!r}", txt)
    dp.end()
    chk(dp.text_of() == "", "结束后文字为空")

    print()
    print("=" * 76)
    print(f"  {PASS} 通过 / {FAIL} 失败")
    print("=" * 76)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
