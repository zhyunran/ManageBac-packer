"""验证：自动刷新间隔真的是 30 分钟（在同一个进程里测）。

背景：`pipeline.snapshot()` 的 `next_refresh_in` 依赖进程内的
      `_state["next_run_ts"]`。在**独立进程**里读它永远是 0，
      因为那个进程没启动自动刷新循环。所以必须在同一进程内测。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from app import config, pipeline  # noqa: E402


def main() -> int:
    print("=" * 64)
    print("  自动刷新间隔验证（同进程）")
    print("=" * 64)

    print(f"\n[1] 配置值")
    print(f"    AUTO_REFRESH_SEC  = {config.AUTO_REFRESH_SEC}")
    print(f"    换算成分钟        = {config.AUTO_REFRESH_SEC / 60:.1f}")
    print(f"    显示文案          = {config.AUTO_REFRESH_TEXT!r}")
    ok_cfg = config.AUTO_REFRESH_SEC == 1800
    print(f"    {'[OK]' if ok_cfg else '[X]'} 是 30 分钟吗")

    print(f"\n[2] 启动自动刷新循环")
    pipeline.load_from_cache()
    pipeline.start_auto_refresh()
    time.sleep(2.5)          # 等循环跑起来

    s = pipeline.snapshot()
    st = pipeline.auto_status()
    print(f"    自动刷新开 : {s['auto_refresh']}")
    print(f"    interval   : {s['interval']} 秒")
    print(f"    interval_text: {s.get('interval_text')!r}")
    print(f"    剩余倒计时 : {s['next_refresh_in']} 秒 "
          f"= {s['next_refresh_in'] / 60:.1f} 分钟")

    remain = s["next_refresh_in"]
    # 刚启动应该接近 1800（允许循环开销）
    ok_remain = 1700 <= remain <= 1800
    print(f"    {'[OK]' if ok_remain else '[X]'} 倒计时接近 30 分钟吗")

    print(f"\n[3] 倒计时在递减吗")
    a = pipeline.snapshot()["next_refresh_in"]
    time.sleep(2.0)
    b = pipeline.snapshot()["next_refresh_in"]
    print(f"    2 秒前: {a}   现在: {b}   差: {b - a} 秒")
    ok_tick = a - b in (1, 2, 3)
    print(f"    {'[OK]' if ok_tick else '[X]'} 正常递减")

    print(f"\n[4] 关闭自动刷新")
    pipeline.set_auto_refresh(False)
    time.sleep(1.2)
    s2 = pipeline.snapshot()
    print(f"    自动刷新开 : {s2['auto_refresh']}")
    print(f"    倒计时     : {s2['next_refresh_in']}")
    ok_off = (s2["auto_refresh"] is False) and s2["next_refresh_in"] == 0
    print(f"    {'[OK]' if ok_off else '[X]'} 关闭生效")

    # 还原
    pipeline.set_auto_refresh(True)

    print(f"\n{'=' * 64}")
    all_ok = ok_cfg and ok_remain and ok_tick and ok_off
    print("  [OK] 全部通过" if all_ok else "  [X] 有问题")
    print(f"{'=' * 64}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
