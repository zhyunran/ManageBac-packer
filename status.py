"""最终状态一览。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from app import files, httpclient, manual_done, store, taskdetail  # noqa: E402

c = store.load_cache() or {}
tasks = c.get("tasks") or []
courses = c.get("courses") or []
lessons = (c.get("schedule") or {}).get("lessons") or []

print("=" * 62)
print("  最终状态")
print("=" * 62)
print(f"  更新时间   : {c.get('updated')}")
print(f"  任务       : {len(tasks)}")
print(f"  课程       : {len(courses)}")
print(f"  课表       : {len(lessons)} 节")
print(f"  有附件     : {sum(1 for x in tasks if x.get('has_attachment'))}")
print(f"  手动标记   : {manual_done.count()} 条")
print(f"  详情缓存   : {len(list(taskdetail.DETAIL_DIR.glob('*.json')))} 个")
print(f"  会话 Cookie: "
      f"{'有' if httpclient.has_saved_session() else '无'}")
cd = httpclient.cooldown_remaining()
print(f"  限速冷却   : {'无' if cd <= 0 else f'{int(cd)} 秒'}")

print()
print("=" * 62)
print("  资料（Files）概况")
print("=" * 62)
for co in courses:
    d = files.list_files(co["class_id"])
    if d.get("ok"):
        nf, nd = len(d.get("files", [])), len(d.get("folders", []))
        if nf or nd:
            print(f"  {co['name'][:40]:42} 文件夹 {nd}  文件 {nf}")

print()
print("=" * 62)
print("  交付清单")
print("=" * 62)
items = [
    ("界面脚本语法", "uv run check_ui.py"),
    ("开机预热", "make_startup.py + VBS(wscript 隐藏窗口)"),
    ("打开即有内容", "widget.py 启动即读缓存"),
    ("自动登录", "credentials.json + httpclient.auto_login"),
    ("我已完成不再提示", "app/manual_done.py"),
    ("作业详情菜单", "app/taskdetail.py + 前端 .dpanel"),
    ("资料（实时网盘）", "app/files.py + 前端第 3 栏"),
    ("下一节课倒计时", "findNextLesson + tickCountdown（跨天/每秒）"),
    ("课表", "schedule.py（token 持久化）"),
]
for name, where in items:
    print(f"   {name:20} {where}")
