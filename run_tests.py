"""离线回归测试入口（发布前跑这个）。

为什么需要它
------------
源码根目录的 13 个 test_*.py 混了两类东西：

  · **离线**：不联网、不需要账号、不需要历史缓存 —— 任何机器上都该通过
  · **在线**：需要 credentials.json、课表登录态、真实缓存或站点可达
              —— 在干净机器上必然失败，失败不代表代码有问题

把两类混在一起跑，就会出现「发布前测试全部通过」这种无法复现的说法：
在开发机上通过是因为**开发机有凭据和数据**，不是因为它可复现。

这个入口只跑第一类，输出一个能在任何机器上复现的数字。

用法
----
    python run_tests.py            # 跑离线用例
    python run_tests.py --all      # 连在线用例一起跑（会看到环境依赖的失败）

退出码
------
    0 = 离线用例全部通过
    1 = 有离线用例失败（这是真问题，不要发布）
    2 = 找不到测试文件
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent

#  在线用例：需要凭据 / 登录态 / 真实缓存 / 站点可达。
#  清单是**显式**的，不靠猜 —— 新增测试时要么进这个集合，要么就该离线可跑。
#
#  划入「离线」的标准是实测出来的：把凭据与缓存都拿掉后仍能以退出码 0 跑完
#  （有内容就断言，没内容就如实跳过）。下面这些做不到，所以算在线。
ONLINE = {
    "test_http.py",             # 纯 HTTP 抓取：直接读 credentials.json，缺了报错
    "test_quiz.py",             # Quiz 分类：直接读 data/cache.json，缺了报错
    "test_schedule.py",         # 课表 API：需要课表登录态
    "test_renew_schedule.py",   # 课表续期：需要登录态
    "test_download.py",         # 下载练习：需要真实附件
    "test_full.py",             # 端到端抓取：需要凭据与网络
}

def discover() -> list[str]:
    return sorted(p.name for p in ROOT.glob("test_*.py"))


def run(name: str) -> tuple[int, str, str]:
    """跑一个测试，返回 (退出码, 最后一行非空输出, 完整输出)。"""
    p = ROOT / name
    try:
        r = subprocess.run(
            [sys.executable, str(p)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        return 124, "超时（180 秒）", ""
    out = (r.stdout or "") + (r.stderr or "")
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    tail = lines[-1] if lines else "(无输出)"
    return r.returncode, tail, out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="连在线用例一起跑")
    args = ap.parse_args()

    names = discover()
    if not names:
        print("找不到 test_*.py")
        return 2

    offline = [n for n in names if n not in ONLINE]
    online = [n for n in names if n in ONLINE]

    print("=" * 70)
    print("  ManageBac-packer 回归测试")
    print("=" * 70)
    print(f"  测试文件    : {len(names)} 个（离线 {len(offline)} / 在线 {len(online)}）")
    print(f"  运行模式    : {'全部' if args.all else '仅离线'}")
    print(f"  解释器      : {sys.executable}")
    print()

    results: list[tuple[str, str, int, str]] = []

    def do(group: list[str], label: str) -> None:
        if not group:
            return
        print(f"── {label} ──")
        for n in group:
            code, tail, _ = run(n)
            status = "OK " if code == 0 else "FAIL"
            results.append((label, n, code, tail))
            shown = tail[:58] + ("…" if len(tail) > 58 else "")
            print(f"  [{status}] {n:24} 码={code:<3} {shown}")
        print()

    do(offline, "离线用例（任何机器都应通过）")
    if args.all:
        do(online, "在线用例（需要凭据 / 登录态 / 真实数据）")

    n_off_fail = sum(1 for g, _, c, _ in results if g.startswith("离线") and c != 0)
    n_off = len(offline)
    n_on_fail = sum(1 for g, _, c, _ in results if g.startswith("在线") and c != 0)

    print("=" * 70)
    print(f"  离线：{n_off - n_off_fail} 通过 / {n_off_fail} 失败   （共 {n_off}）")
    if args.all:
        print(f"  在线：{len(online) - n_on_fail} 通过 / {n_on_fail} 失败   （共 {len(online)}）")
        print()
        print("  说明：在线用例失败通常只是本机缺少凭据 / 登录态 / 历史缓存，")
        print("        不代表代码有问题。要判定是否回归，请与上一版在同一台机器上对比。")
    else:
        print(f"  未运行在线用例 {len(online)} 个（加 --all 可跑）")
    print("=" * 70)

    if n_off_fail:
        print("\n  [X] 有离线用例失败 —— 不要发布。")
        return 1
    print("\n  [OK] 离线用例全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
