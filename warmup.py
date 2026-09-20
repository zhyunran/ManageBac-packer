"""预热入口 —— 自动登录 + 抓取，让小组件「打开即有内容」。

用法：
    uv run warmup.py                # 缓存旧了才抓
    uv run warmup.py --force        # 强制抓一次
    uv run warmup.py --quiet        # 静默（开机启动用）

开机启动会调用本脚本（见 make_startup.py）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import warmup  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(warmup.main())
