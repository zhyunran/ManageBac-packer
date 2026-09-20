"""完整端到端测试：成绩 + 课表 一次抓取。"""
import sys
import threading
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import pipeline

print("=" * 72)
print("  完整抓取（成绩 + 课表）")
print("=" * 72)

t = threading.Thread(target=pipeline.refresh_sync, daemon=True)
t.start()
t.join(timeout=360)

if t.is_alive():
    print("[!] 超时（仍在后台运行）")

s = pipeline.snapshot()
print()
print(f"状态   : {s['status']}")
print(f"消息   : {s['message']}")
print(f"更新时间: {s['updated']}")
print(f"任务   : {len(s['tasks'])}")
print(f"课程   : {len(s['courses'])}")

sc = s.get("schedule") or {}
lessons = sc.get("lessons") or []
print(f"课表   : ready={sc.get('ready')}  {len(lessons)} 节")
if sc.get("error"):
    print(f"         错误: {sc['error']}")

if lessons:
    days = sorted({l["day"] for l in lessons})
    eve = [l for l in lessons if l["kind"] == "晚自习"]
    print(f"         覆盖 {len(days)} 天: {days[0]} ~ {days[-1]}")
    print(f"         晚自习 {len(eve)} 节")

    today = days[0] if days else ""
    import datetime
    td = datetime.date.today().isoformat()
    td_lessons = [l for l in lessons if l["day"] == td]
    print()
    print(f"  今日（{td}）{len(td_lessons)} 节：")
    for l in td_lessons:
        st = l["start"][11:16]
        en = l["end"][11:16] if len(l["end"]) >= 16 else ""
        print(f"     {st}-{en}  {l['kind']:<4} {l['subject'][:20]:<20} "
              f"{l['room'][:10]:<10} {l['teacher'][:12]}")

print()
print("=" * 72)
