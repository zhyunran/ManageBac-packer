"""课表网站（yly.seiue.com）登录 + 接口侦察。

    uv run schedule_login.py

流程：
  1. 打开一个**真实 Edge 窗口**（无自动化特征，不会被风控识别）
  2. **你自己**在窗口里输入账号密码登录 —— 程序不接触密码
  3. 登录后进入课表页，程序自动记录所有 XHR 请求
  4. 按回车结束，生成接口清单

产物（data/schedule_recon/）：
    summary.txt   接口清单（先看这个）
    api.json      原始记录
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import cdp, config  # noqa: E402

SCHEDULE_URL = "https://yly.seiue.com/"
OUT = config.DATA_DIR / "schedule_recon"
PROFILE = config.DATA_DIR / "schedule-profile"     # 与 ManageBac 隔离

SKIP_EXT = re.compile(r"\.(png|jpe?g|gif|svg|woff2?|ttf|eot|ico|css|mp4|webp|map)(\?|$)", re.I)


def main() -> int:
    config.ensure_dirs()
    OUT.mkdir(parents=True, exist_ok=True)

    print("=" * 76)
    print("  课表网站登录 + 接口侦察")
    print("=" * 76)
    print(f"  网站     : {SCHEDULE_URL}")
    print(f"  数据目录 : {PROFILE}")
    print()

    if cdp.is_running(PROFILE):
        print("  [i] 该数据目录的浏览器已在运行，将复用。")
    else:
        print("  正在打开 Edge 窗口……")
        cdp.launch(PROFILE, headless=False, url=SCHEDULE_URL)

    port = cdp.wait_for_port(PROFILE, timeout=50)
    conn = cdp.CDP(port)

    # 找课表标签页
    target_id = None
    for _ in range(20):
        for t in conn.targets():
            if t.get("type") == "page" and "seiue" in (t.get("url") or "").lower():
                target_id = t["targetId"]
                break
        if target_id:
            break
        time.sleep(0.5)
    if target_id is None:
        pages = [t for t in conn.targets() if t.get("type") == "page"]
        if pages:
            target_id = pages[0]["targetId"]
    if target_id is None:
        print("  [X] 找不到可用标签页")
        return 1

    sid = conn.attach(target_id)
    page = cdp.Page(conn, sid, target_id)
    try:
        conn.send("Network.enable", {}, session_id=sid)
        print("  [OK] 网络监听已开启")
    except Exception as e:
        print(f"  [!] 网络监听失败（改用 Performance API 兜底）：{e}")

    print()
    print("  ┌──────────────────────────────────────────────────────────────┐")
    print("  │  请在 Edge 窗口里输入你的账号密码登录。                       │")
    print("  │  登录后【打开课表页面】，让数据加载出来。                     │")
    print("  │  程序会自动记录所有接口。                                     │")
    print("  │  完成后回到这里按回车结束。                                   │")
    print("  └──────────────────────────────────────────────────────────────┘")
    print()
    print("  开始记录……（会持续 15 分钟或直到你按回车）")
    print()

    records: dict[str, dict] = {}

    def ingest(events: list[dict]) -> None:
        for ev in events:
            m = ev.get("method")
            if m == "Network.responseReceived":
                p = ev.get("params", {})
                resp = p.get("response", {}) or {}
                url = resp.get("url") or ""
                if not url or SKIP_EXT.search(url) or url in records:
                    continue
                records[url] = {
                    "url": url, "status": resp.get("status"),
                    "mime": resp.get("mimeType") or "",
                    "method": "GET",
                }
            elif m == "Network.requestWillBeSent":
                p = ev.get("params", {})
                req = p.get("request", {}) or {}
                url = req.get("url") or ""
                if not url or SKIP_EXT.search(url) or url in records:
                    continue
                if req.get("method", "GET") != "GET":
                    records[url] = {"url": url, "status": None,
                                    "mime": "", "method": req.get("method", "")}

    t0 = time.time()
    last_note = 0
    try:
        while time.time() - t0 < 900:
            conn.pump(iterations=30, timeout_each=0.05)
            ingest(conn.drain_events())

            # 兜底：Performance API
            try:
                raw = page.eval("""
                    JSON.stringify(performance.getEntriesByType('resource')
                      .filter(e => e.initiatorType === 'xmlhttprequest'
| e.initiatorType === 'fetch')
                      .map(e => e.name))
                """)
                if raw:
                    for u in json.loads(raw):
                        if u not in records and not SKIP_EXT.search(u):
                            records[u] = {"url": u, "status": None,
                                          "mime": "", "method": "GET"}
            except Exception:
                pass

            el = int(time.time() - t0)
            if el - last_note >= 20:
                last_note = el
                print(f"     … 已记录 {len(records)} 个请求（{el}s）")

            try:
                import msvcrt
                if msvcrt.kbhit():
                    msvcrt.getch()
                    break
            except Exception:
                pass
    except KeyboardInterrupt:
        pass

    conn.close()

    # ---- 保存 ----
    report = []
    for u, r in records.items():
        p = urlparse(u)
        report.append({**r, "host": p.netloc, "path": p.path, "query": p.query})

    (OUT / "api.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = ["=" * 78, "  课表网站接口清单", "=" * 78]
    groups: dict[str, list[dict]] = {}
    for r in report:
        groups.setdefault(r["host"], []).append(r)
    for host, items in groups.items():
        lines.append("")
        lines.append(f"【{host}】{len(items)} 个请求")
        for it in items:
            q = f"?{it['query'][:80]}" if it["query"] else ""
            st = f"{it['status']}" if it.get("status") else "-"
            lines.append(f"  [{st}] {it['method'][:6]:<6} {it['path'][:70]}{q}")
            lines.append(f"          {it['url'][:140]}")
    (OUT / "summary.txt").write_text("\n".join(lines), encoding="utf-8")

    print()
    print("=" * 76)
    print(f"  [OK] 记录 {len(report)} 个请求")
    print(f"       清单：{OUT / 'summary.txt'}")
    print("=" * 76)
    return 0


if __name__ == "__main__":
    sys.exit(main())
