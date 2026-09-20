"""测试任务详情抓取（用真实任务）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from app import store, taskdetail  # noqa: E402

c = store.load_cache() or {}
tasks = c.get("tasks") or []
print(f"缓存里共 {len(tasks)} 个任务\n")

# 找出有附件的和没附件的各一个
withatt = [t for t in tasks if t.get("has_attachment")]
noatt = [t for t in tasks if not t.get("has_attachment")]
print(f"标记有附件的任务: {len(withatt)} 个")
for t in withatt[:6]:
    print(f"  - {t['title'][:44]}")
print(f"\n未标记附件的任务: {len(noatt)} 个")

# 挑几个来实测（有附件的优先）
picks = (withatt[:3] + noatt[:2])[:4]
if not picks:
    print("\n缓存里没有任务，跳过")
    raise SystemExit(0)

print("\n" + "=" * 70)
print("  实测抓取详情")
print("=" * 70)
for t in picks:
    print(f"\n▸ {t['title'][:52]}")
    print(f"  URL: {t['url'][-48:]}")
    d = taskdetail.fetch(t["url"])
    if not d.get("ok"):
        print(f"  [X] {d.get('error')}")
        continue
    print(f"  title       : {d.get('title', '')[:52]}")
    print(f"  attachments : {len(d.get('attachments', []))} 个")
    for a in d.get("attachments", [])[:4]:
        print(f"      • {a['name'][:56]}")
    desc = d.get("description", "")
    print(f"  description : {len(desc)} 字"
          + (f"  「{desc[:70]}…」" if desc else "  （空）"))
    g = d.get("grade", {})
    print(f"  grade       : {g.get('text') or '（无）'}")
    print(f"  due         : {d.get('due_text') or '（无）'}")
    print(f"  cached      : {d.get('cached', False)}")

print("\n" + "=" * 70)
print("  第二次读缓存（应该几乎立即）")
print("=" * 70)
import time  # noqa: E402

t0 = time.time()
d2 = taskdetail.fetch(picks[0]["url"])
print(f"  耗时 {time.time()-t0:.3f} 秒   cached={d2.get('cached')}")
