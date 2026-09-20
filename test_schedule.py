"""测试课表抓取（直接调用 API）。"""
import json
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import config
from app.schedule import api_get, fetch_schedule, read_session

OUT = config.DATA_DIR / "schedule_recon"
OUT.mkdir(parents=True, exist_ok=True)

print("=" * 76)
print("  课表抓取测试")
print("=" * 76)

print("\n[1] 读取会话……")
sess = read_session()
for k, v in sess.items():
    if k == "access_token":
        print(f"    {k}: {str(v)[:40]}…（{len(str(v))} 字符）")
    else:
        print(f"    {k}: {v}")

if sess.get("error"):
    print(f"\n[X] {sess['error']}")
    sys.exit(1)

print("\n[2] 调用课表接口（今天）……")
import datetime

d = datetime.date.today().isoformat()
status, text = api_get("/scms/timetable/structure", sess["access_token"], {
    "date": d,
    "reflection_id": sess["reflection_id"],
    "resolve_most_used_timetable_id_by_week": "true",
})
print(f"    HTTP {status}  {len(text)} 字符")
if status == 200:
    try:
        data = json.loads(text)
        (OUT / "timetable_sample.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"    → 已保存 timetable_sample.json")
        if isinstance(data, dict):
            print(f"    顶层键: {list(data.keys())}")
            for k, v in data.items():
                if isinstance(v, list):
                    print(f"      {k}: {len(v)} 项")
                    if v and isinstance(v[0], dict):
                        print(f"         首项键: {list(v[0].keys())[:20]}")
                        print(f"         首项: {json.dumps(v[0], ensure_ascii=False)[:280]}")
                elif isinstance(v, dict):
                    print(f"      {k}: dict, 键={list(v.keys())[:12]}")
        elif isinstance(data, list):
            print(f"    列表 {len(data)} 项")
            if data and isinstance(data[0], dict):
                print(f"      首项键: {list(data[0].keys())[:20]}")
                print(f"      首项: {json.dumps(data[0], ensure_ascii=False)[:300]}")
    except Exception as e:
        print(f"    解析失败: {e}")
        print(f"    原文: {text[:300]}")
else:
    print(f"    内容: {text[:300]}")

print("\n[3] 完整抓取（未来 7 天 + 晚自习）……")
sch = fetch_schedule(days_ahead=7)
print(f"    ready={sch.ready}  课程={len(sch.lessons)} 节")
if sch.error:
    print(f"    error: {sch.error}")

if sch.lessons:
    print()
    by_day = {}
    for l in sch.lessons:
        by_day.setdefault(l.day, []).append(l)
    for day in sorted(by_day)[:9]:
        print(f"  ── {day} ──")
        for l in by_day[day]:
            print(f"     {l.start[11:16] if len(l.start)>=16 else l.start:<5}"
                  f" – {l.end[11:16] if len(l.end)>=16 else '':<5}"
                  f" {l.kind:<4} {l.subject[:30]:<30} {l.room}")

print()
print("=" * 76)
