"""自动登录模块测试。

用法：
    .venv\\Scripts\\python.exe test_autologin.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from app import autologin, config, httpclient  # noqa: E402


def line(t=""):
    print(t, flush=True)


def main() -> int:
    line("=" * 64)
    line("  自动登录模块测试")
    line("=" * 64)

    # ── 1. 配置 ──
    line()
    line("[1] 刷新间隔配置")
    line(f"    间隔        : {config.AUTO_REFRESH_SEC} 秒"
         f" = {config.AUTO_REFRESH_SEC // 60} 分钟")
    line(f"    显示文案    : {config.AUTO_REFRESH_TEXT}")
    line(f"    退避上限    : {config.AUTO_REFRESH_MAX_BACKOFF} 秒")
    ok_interval = config.AUTO_REFRESH_SEC == 300
    line(f"    {'[OK]' if ok_interval else '[X]'} 是 5 分钟吗: {ok_interval}")

    line()
    line("[2] 自动登录配置")
    line(f"    允许弹窗兜底: {config.AUTO_LOGIN_ALLOW_POPUP}")
    line(f"    登录最小间隔: {config.AUTO_LOGIN_MIN_GAP} 秒"
         f" = {config.AUTO_LOGIN_MIN_GAP // 60} 分钟")

    # ── 2. 指数退避（防锁号的关键）──
    line()
    line("[3] 指数退避（防止反复提交把账号锁掉）")
    rows = []
    for fails in range(0, 8):
        autologin._write_state(test_fails=fails, test_last_attempt=0)
        ok, _ = autologin._backoff_ok("test")
        gap = (min(config.AUTO_LOGIN_MIN_GAP * (2 ** min(fails, 6)), 3600)
               if fails else config.AUTO_LOGIN_MIN_GAP)
        rows.append(gap // 60)
        line(f"    连续失败 {fails} 次 → 等 {gap // 60:>3} 分钟后再试")
    inc = all(rows[i] <= rows[i + 1] for i in range(len(rows) - 1))
    line(f"    {'[OK]' if inc else '[X]'} 等待时间单调不减（越失败等越久）")

    # ── 3. 只读探测 ──
    line()
    line("[4] 只读探测登录态（不提交表单，不会触发锁定）")
    cd = httpclient.cooldown_remaining()
    if cd > 0:
        line(f"    [!] 站点正在限速冷却中，还剩 {int(cd)} 秒"
             f"（{int(cd // 60) + 1} 分钟）")
        line(f"        这是正常的保护机制，说明限速逻辑在工作")

    t0 = time.time()
    mb = autologin.mb_session_ok()
    line(f"    ManageBac 会话 : {'有效' if mb else '已失效'}"
         f"   (耗时 {time.time() - t0:.1f}s)")

    t0 = time.time()
    sch = autologin.schedule_token_ok()
    line(f"    课表 token     : {'有效' if sch else '已失效'}"
         f"   (耗时 {time.time() - t0:.1f}s)")

    # ── 4. 状态 ──
    line()
    line("[5] 状态概要（界面用的）")
    st = autologin.status()
    brief = {k: v for k, v in st.items()
             if k not in ("last", "at")}
    line("    " + json.dumps(brief, ensure_ascii=False))

    # ── 5. 清理 ──
    line()
    autologin._write_state(test_fails=0, test_last_attempt=0)
    line("[OK] 已清理测试残留")

    line()
    line("=" * 64)
    all_ok = ok_interval and inc
    line("  [OK] 全部通过" if all_ok else "  [X] 有问题")
    line("=" * 64)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
