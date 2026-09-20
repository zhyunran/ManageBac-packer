"""验证 Quiz 分类逻辑（用真实缓存数据）。"""
import json
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import config

cache = json.loads(config.CACHE_FILE.read_text(encoding="utf-8"))
tasks = [t for t in cache["tasks"] if not t.get("completed")]

# 与前端一致的判定规则
def is_quiz(t):
    s = ((t.get("title") or "") + " " + (t.get("kind") or "")).lower()
    return bool(re.search(r"quiz|测验|小测|test\b", s))

def is_homework(t):
    s = ((t.get("kind") or "") + " " + (t.get("title") or "")).lower()
    return bool(re.search(r"homework|作业|hw\b", s))

quiz = [t for t in tasks if is_quiz(t)]
hw = [t for t in tasks if not is_quiz(t) and is_homework(t)]
other = [t for t in tasks if not is_quiz(t) and not is_homework(t)]

print("=" * 76)
print(f"  Quiz 分类验证（待办共 {len(tasks)} 项）")
print("=" * 76)

print(f"\n【测验 Quiz】{len(quiz)} 项")
for t in quiz:
    print(f"   · {t['title'][:44]:<44} {t['kind'] or '':<10} {t['course'][:16]}")

print(f"\n【作业 Homework】{len(hw)} 项")
for t in hw:
    print(f"   · {t['title'][:44]:<44} {t['kind'] or '':<10} {t['course'][:16]}")

print(f"\n【其他】{len(other)} 项")
for t in other[:12]:
    print(f"   · {t['title'][:44]:<44} {t['kind'] or '':<10} {t['course'][:16]}")
if len(other) > 12:
    print(f"   … 还有 {len(other)-12} 项")

print()
print("=" * 76)
print(f"  合计 {len(quiz)} + {len(hw)} + {len(other)} = {len(quiz)+len(hw)+len(other)}"
      f"  （应为 {len(tasks)}）")
print("=" * 76)
