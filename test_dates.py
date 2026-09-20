"""日期推断的边界用例测试（离线，不联网）。"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.scraper import resolve_year

TODAY = date(2026, 9, 17)          # 固定参考日，便于复现

CASES = [
    # (月, 日, 期望年份, 说明)
    (9, 11, 2026, "刚过去 6 天 → 今年"),
    (9, 21, 2026, "4 天后 → 今年"),
    (1, 17, 2027, "跨年未来 → 明年（用户指出的古诗背诵）"),
    (1, 5, 2027, "明年 1 月 → 明年"),
    (12, 25, 2026, "今年 12 月 → 今年"),
    (10, 1, 2026, "下月 → 今年"),
    (8, 20, 2026, "上月 → 今年"),
    (9, 1, 2026, "本月初 → 今年"),
    (9, 17, 2026, "今天 → 今年"),
    (6, 1, 2026, "-108 天 vs +257 天 → 取更近的今年"),
    (4, 1, 2026, "-169 天 vs +196 天 → 取更近的今年"),
    (6, 15, 2026, "-94 天 vs +271 天 → 取更近的今年"),
    (11, 1, 2026, "+45 天 → 今年"),
    (2, 1, 2027, "-228 天 vs +137 天 → 取更近的明年"),
]

fail = 0
print("=" * 72)
print(f"  日期推断测试（参考日 {TODAY}）")
print("=" * 72)
print(f"  {'月/日':<8}{'结果':<14}{'相对天数':<12}{'期望':<8}{'':<4}说明")
print("  " + "-" * 66)
for mo, d, want, note in CASES:
    y = resolve_year(mo, d, TODAY)
    got = date(y, mo, d)
    delta = (got - TODAY).days
    ok = (y == want)
    if not ok:
        fail += 1
    print(f"  {mo}/{d:<6}{got.isoformat():<14}{delta:>+6d} 天"
          f"{'':<4}{want:<8}{'OK' if ok else 'FAIL':<5}{note}")

print()
print("=" * 72)
print(f"  {'全部通过' if not fail else f'{fail} 项失败'}")
print("=" * 72)
sys.exit(1 if fail else 0)
