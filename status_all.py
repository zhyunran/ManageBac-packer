"""检查当前状态：课表 + 晚报 + 登录态。"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from app import autologin, config, digest, httpclient, pipeline  # noqa: E402


def main() -> int:
    print("=" * 64)
    print("  当前状态总览")
    print("=" * 64)

    pipeline.load_from_cache()
    s = pipeline.snapshot()
    sch = s.get("schedule") or {}

    print("\n[1] 数据")
    print(f"    状态      : {s.get('status')}")
    print(f"    消息      : {s.get('message')}")
    print(f"    更新时间  : {s.get('updated')}")
    print(f"    任务数    : {len(s.get('tasks') or [])}")
    print(f"    课程数    : {len(s.get('courses') or [])}")

    print("\n[2] 刷新设置")
    print(f"    自动刷新  : {'开' if s.get('auto_refresh') else '关'}")
    print(f"    间隔      : {s.get('interval')} 秒 "
          f"= {(s.get('interval') or 0) // 60} 分钟")
    print(f"    下次刷新  : {s.get('next_refresh_in')} 秒后")

    print("\n[3] 课表")
    print(f"    ready     : {sch.get('ready')}")
    print(f"    节数      : {len(sch.get('lessons') or [])}")
    if sch.get("error"):
        print(f"    错误      : {sch['error']}")
    lessons = sch.get("lessons") or []
    if lessons:
        for l in lessons[:3]:
            print(f"      · {l.get('day')} {l.get('start')} → {l.get('end')} "
                  f"{l.get('subject')} @{l.get('room')}")

    print("\n[4] 登录态")
    cd = httpclient.cooldown_remaining()
    if cd > 0:
        print(f"    ManageBac 限速冷却中：还剩 {int(cd)} 秒")
    print(f"    ManageBac 会话 : "
          f"{'有效' if autologin.mb_session_ok() else '已失效'}")
    print(f"    课表 token     : "
          f"{'有效' if autologin.schedule_token_ok() else '已失效'}")
    tok = autologin._read_state()
    print(f"    自动登录失败数 : mb={tok.get('mb_fails', 0)} "
          f"sch={tok.get('sch_fails', 0)}")

    print("\n[5] 晚报")
    try:
        items = digest.list_all(5)
        print(f"    已有期数  : {len(items)}")
        if items:
            x = items[0]
            g = x.get("gpa") or {}
            c = x.get("counts") or {}
            print(f"    最新一期  : {x.get('date')} {x.get('weekday')}")
            print(f"    总 GPA    : {g.get('gpa')} "
                  f"({g.get('letter')}, {g.get('percent')}%)")
            print(f"    新增作业  : {c.get('new_tasks')}")
            print(f"    新增成绩  : {c.get('new_grades')}")
            print(f"    今日完成  : {c.get('new_done')}")
        else:
            print("    (还没生成 —— 工作日晚 9 点后打开就会生成)")
        st = digest.status()
        print(f"    未读      : {st.get('unread')}")
    except Exception as e:
        print(f"    异常：{e}")

    print("\n" + "=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
