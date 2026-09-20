"""实测：打开任务详情页到底慢在哪一步。

用法：
    .venv\\Scripts\\python.exe diag_detail_speed.py

会分阶段计时：
    ① 读缓存
    ② 建会话（装载 Cookie）
    ③ 登录态检查（check_login —— 这一步会发一个请求！）
    ④ 请求详情页 HTML
    ⑤ 解析 HTML
    ⑥ 写缓存
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from app import config, httpclient, pipeline, taskdetail  # noqa: E402


def ms(t: float) -> str:
    return f"{t * 1000:.0f} ms"


def main() -> int:
    print("=" * 68)
    print("  任务详情页速度诊断")
    print("=" * 68)

    # 找几个真实任务
    pipeline.load_from_cache()
    s = pipeline.snapshot()
    tasks = [t for t in (s.get("tasks") or []) if t.get("url")]
    if not tasks:
        print("\n[X] 缓存里没有任务，先跑一次抓取")
        return 1

    print(f"\n[0] 缓存里有 {len(tasks)} 个任务")
    sample = tasks[:3]

    # ── ① 缓存命中测试 ──
    print(f"\n[1] 缓存命中（应该极快）")
    hits = 0
    for t in sample:
        tid = taskdetail._tid_of(t["url"])
        t0 = time.perf_counter()
        c = taskdetail._read_cache(tid)
        dt = time.perf_counter() - t0
        flag = "命中" if c else "未缓存"
        if c:
            hits += 1
        print(f"    {tid:>10}  {flag}   {ms(dt)}")
    print(f"    命中 {hits}/{len(sample)}")

    # ── ② 强制不走缓存，测真实的 fetch() ──
    print(f"\n[2] 真实 fetch()（force=True，不走缓存）")
    print(f"    用第一个任务：{sample[0].get('title', '')[:40]}")

    url = sample[0]["url"]
    tid = taskdetail._tid_of(url)

    # 分段计时内部步骤（手动复现 fetch 的流程，用于定位）
    t0 = time.perf_counter()
    s = httpclient.HttpSession(auto_login_on_demand=True)
    dt_sess = time.perf_counter() - t0

    t0 = time.perf_counter()
    s.open(verify=False)             #  新路径：不额外探测
    dt_open = time.perf_counter() - t0

    t0 = time.perf_counter()
    page = s.page()
    page.goto(url)
    html = page.content()
    dt_goto = time.perf_counter() - t0

    t0 = time.perf_counter()
    data = taskdetail.parse_detail(html, url)
    dt_parse = time.perf_counter() - t0

    t0 = time.perf_counter()
    taskdetail._write_cache(tid, data)
    dt_write = time.perf_counter() - t0

    t0 = time.perf_counter()
    s.close()
    dt_close = time.perf_counter() - t0

    print(f"    A. 新建 HttpSession      {ms(dt_sess)}")
    print(f"    B. open(verify=False)    {ms(dt_open)}  ← 已不探测登录")
    print(f"    C. 请求详情页 HTML       {ms(dt_goto)}  "
          f"（{len(html):,} 字节）")
    print(f"    D. BeautifulSoup 解析    {ms(dt_parse)}")
    print(f"    E. 写缓存                {ms(dt_write)}")
    print(f"    F. close()               {ms(dt_close)}")

    total = dt_sess + dt_open + dt_goto + dt_parse + dt_write + dt_close
    print(f"    ─────────────────────────────────")
    print(f"    合计                     {ms(total)}")

    # ── ③ 真正端到端（调真实函数）──
    print(f"\n[3] 端到端：真实 fetch() 函数")
    taskdetail.clear_cache()
    t0 = time.perf_counter()
    r = taskdetail.fetch(sample[1]["url"])
    dt_real = time.perf_counter() - t0
    print(f"    fetch()  {ms(dt_real)}   ok={r.get('ok')}  "
          f"cached={r.get('cached')}")
    print(f"    标题：{r.get('title', '')[:40]}")
    print(f"    附件：{len(r.get('attachments') or [])} 个")

    # ── ④ 瓶颈排序 ──
    print(f"\n[4] 瓶颈排序")
    parts = [
        ("请求详情页", dt_goto),
        ("open（装载 Cookie）", dt_open),
        ("解析 HTML", dt_parse),
        ("新建会话", dt_sess),
        ("写缓存", dt_write),
        ("close()", dt_close),
    ]
    parts.sort(key=lambda x: -x[1])
    for name, dt in parts:
        pct = dt / total * 100 if total else 0
        bar = "#" * int(pct / 4)
        print(f"    {name:<22} {ms(dt):>10}  {pct:>5.1f}%  {bar}")

    # ── ⑤ 对比：第二次（缓存已写）──
    print(f"\n[5] 第二次打开同一个任务（缓存应命中）")
    t0 = time.perf_counter()
    r2 = taskdetail.fetch(sample[1]["url"])
    dt2 = time.perf_counter() - t0
    print(f"    fetch()  {ms(dt2)}   cached={r2.get('cached')}")

    print(f"\n{'=' * 68}")
    print(f"  首次打开（分段合计）: {total:.2f}s")
    print(f"  首次打开（端到端）  : {dt_real:.2f}s")
    if dt2 < 0.05:
        print(f"  [OK] 缓存生效（第二次 {ms(dt2)}）")
    else:
        print(f"  [!] 第二次也没走缓存（{ms(dt2)}）")
    print(f"{'=' * 68}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
