"""测试：课表登录态自动续期。

用法：
    .venv\\Scripts\\python.exe test_renew_schedule.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from app import autologin, schedule  # noqa: E402


def main() -> int:
    print("=" * 64)
    print("  课表登录态自动续期测试")
    print("=" * 64)

    # ── 0. 清掉退避状态（测试用）──
    print("\n[0] 重置退避计时（否则上次的失败会挡住本次测试）")
    autologin._write_state(sch_fails=0, sch_last_attempt=0)
    print("    [OK] 已重置")

    # ── 续期前 ──
    print("\n[1] 续期前状态")
    before = schedule.load_token()
    print(f"    有 token       : {bool(before.get('access_token'))}")
    print(f"    saved_at 距今  : "
          f"{(time.time() - float(before.get('saved_at') or 0)) / 3600:.1f} 小时")
    tok = before.get("access_token") or ""
    print(f"    JWT 已过期吗   : {autologin.jwt_expired(tok)}")
    ok0 = autologin.schedule_token_ok()
    print(f"    token 能用吗   : {'有效' if ok0 else '已失效'}")

    if ok0:
        print("\n    token 还有效，不需要续期。")
        print("    （想强制测试的话，先删掉 data/schedule_token.json）")
        return 0

    # ── 续期 ──
    print("\n[2] 开始自动续期（无头浏览器，约 30~90 秒）")
    print("    过程：启动无头 Edge → 清掉旧会话 → 填表 → 等新 token → 保存")
    t0 = time.time()
    ok, msg = autologin.renew_schedule()
    dt = time.time() - t0
    print(f"\n    结果   : {'[OK] 成功' if ok else '[X] 失败'}")
    print(f"    耗时   : {dt:.1f}s")
    print(f"    说明   : {msg}")

    # ── 续期后 ──
    print("\n[3] 续期后状态")
    after = schedule.load_token()
    print(f"    有 token       : {bool(after.get('access_token'))}")
    if after.get("saved_at") != before.get("saved_at"):
        print(f"     token 已更新（saved_at 变了）")
    print(f"    reflection_id  : {after.get('reflection_id')!r}")
    print(f"    用户           : {after.get('user_name')!r}")

    ok1 = autologin.schedule_token_ok()
    print(f"    token 能用吗   : {'[OK] 有效' if ok1 else '[X] 仍失效'}")

    # ── 真的抓一次课表 ──
    print("\n[4] 用新 token 抓一次课表")
    try:
        sch = schedule.fetch_schedule()
        print(f"    ready   : {sch.ready}")
        print(f"    课程数  : {len(sch.lessons or [])}")
        if sch.error:
            print(f"    错误    : {sch.error}")
        if sch.lessons:
            for l in sch.lessons[:3]:
                print(f"      · {l.day} {l.start[:5]}-{l.end[:5]} "
                      f"{l.subject} {l.room}")
    except Exception as e:
        print(f"    异常：{type(e).__name__}: {e}")

    print("\n" + "=" * 64)
    all_ok = ok1
    print("  [OK] 自动续期链路打通" if all_ok else "  [X] 仍未成功")
    print("=" * 64)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
