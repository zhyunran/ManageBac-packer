"""验证「我已完成，不再提示」模块。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from app import manual_done as md          # noqa: E402
from app.models import Task                # noqa: E402

print("=" * 68)
print("  1. 键生成")
print("=" * 68)
cases = [
    ("https://x/core_tasks/123456", "Essay", "ELA"),
    ("", "Materials Check", "Physics"),
    ("https://x/student/classes/1", "", ""),
]
for url, title, course in cases:
    print(f"  {md.key_for(url, title, course)!r}")
    print(f"      url={url[-24:]!r}  title={title!r}")

print()
print("=" * 68)
print("  2. 标记 / 查询")
print("=" * 68)
U = "https://your-school.managebac.cn/student/classes/999/core_tasks/777001"
md.clear_all()
print(f"  标记     : {md.mark(U, '纸质版论文', 'ELA')}")
print(f"  已标记?  : {md.is_marked(U, '纸质版论文', 'ELA')}")
print(f"  总数     : {md.count()}")

print()
print("=" * 68)
print("  3. 应用到任务列表")
print("=" * 68)
tasks = [
    Task(title="纸质版论文", url=U, course="ELA",
         submitted=False, graded=False, completed=False),
    Task(title="正常作业", url="https://x/core_tasks/888", course="Math",
         submitted=True, completed=True),
]
n = md.apply(tasks)
print(f"  命中 {n} 条")
for t in tasks:
    print(f"    {t.title:12}  completed={t.completed!s:5}  manual={t.manual!s:5}")

print()
print("=" * 68)
print("  4. 撤销后应恢复未完成")
print("=" * 68)
md.unmark(U, "纸质版论文", "ELA")
tasks2 = [Task(title="纸质版论文", url=U, course="ELA",
               submitted=False, graded=False, completed=True, manual=True)]
md.apply(tasks2)
t2 = tasks2[0]
ok = (t2.completed is False and t2.manual is False)
print(f"  completed={t2.completed}  manual={t2.manual}")
print(f"  {'[OK] 已恢复为未完成' if ok else '[X] 恢复失败'}")

print()
print("=" * 68)
print("  5. 持久化文件")
print("=" * 68)
md.mark(U, "纸质版论文", "ELA")
print(f"  路径 : {md.FILE}")
print(f"  存在 : {md.FILE.exists()}")
print(f"  内容 : {json.dumps(md.all_marks(), ensure_ascii=False)}")

print()
print("=" * 68)
print("  6. 按标题兜底（无任务 ID 时）")
print("=" * 68)
md.clear_all()
U2 = "https://x/student/classes/1"
print(f"  标记 : {md.mark(U2, 'Reading Log', 'English')}")
t3 = Task(title="Reading Log", url=U2, course="English", completed=False)
md.apply([t3])
print(f"  completed={t3.completed} manual={t3.manual}")
print(f"  {'[OK] 标题兜底可用' if t3.completed else '[X] 兜底失败'}")

# 收尾：清掉测试数据
md.clear_all()
print()
print("[已清空测试数据]")
