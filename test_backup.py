"""备份模块自测入口（从项目根跑）。

用法：
    .venv\\Scripts\\python.exe test_backup.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from app import backup  # noqa: E402

if __name__ == "__main__":
    sys.exit(backup._self_test())
