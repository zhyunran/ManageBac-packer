"""侦察 Files 页面（动态渲染后）与任务详情页附件。

关键：Files 页面内容可能是 JS 异步加载的，必须在浏览器里等它渲染完再读。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import cdp, config  # noqa: E402

OUT = config.DATA_DIR / "attach_recon"
OUT.mkdir(parents=True, exist_ok=True)

IDS = "11516047"
PHYS = "11516114"


def main() -> int:
    if not cdp.is_running(config.BROWSER_PROFILE_DIR):
        print("[X] 浏览器未运行")
        return 2

    session = cdp.Session(headless=True)
    session.open()
    try:
        page = session.page()
        page.goto(f"{config.BASE_URL}/student/home")
        page.wait_for_timeout(1500)
        if not cdp.looks_logged_in(page):
            print("[X] 未登录")
            return 2

        # ============ 1. Files 页面：监听网络 + 等渲染 ============
        print("=" * 78)
        print("  【1】Files 页面")
        print("=" * 78)
        url = f"{config.BASE_URL}/student/classes/{IDS}/files"
        page.goto(url)
        page.wait_for_timeout(5000)     # 多等一会，内容通常是异步加载

        # 读取页面内已发生的 XHR
        js = """
        JSON.stringify(performance.getEntriesByType('resource')
          .filter(e => ['xmlhttprequest','fetch'].includes(e.initiatorType))
          .map(e => e.name)
          .filter(u => /files?|folder|resource|document/i.test(u)))
        """
        try:
            raw = page.eval(js)
            xhr = json.loads(raw) if raw else []
            print(f"  相关 XHR {len(xhr)} 个：")
            for u in xhr[:20]:
                print(f"    {u[:130]}")
            if xhr:
                (OUT / "files_xhr.json").write_text(json.dumps(xhr, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            print(f"  XHR 读取失败：{e}")

        # DOM 里的文件区域
        print()
        print("  页面内文件/文件夹元素：")
        js2 = """
        (() => {
          const out = [];
          document.querySelectorAll('a,div,tr,li').forEach(el => {
            const h = el.getAttribute && el.getAttribute('href');
            const t = (el.innerText || '').trim();
            if (h && /download|files?\\/|folder/i.test(h) && t && t.length < 90) {
              out.push({h, t});
            }
          });
          return JSON.stringify(out.slice(0, 40));
        })()
        """
        try:
            raw = page.eval(js2)
            items = json.loads(raw) if raw else []
            print(f"  找到 {len(items)} 个")
            for it in items[:20]:
                print(f"    {it['h'][:88]}")
                print(f"        «{it['t'][:56]}»")
            (OUT / "files_dom.json").write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            print(f"  DOM 读取失败：{e}")

        html = page.content()
        (OUT / "files_rendered.html").write_text(html, encoding="utf-8")
        print(f"\n  HTML {len(html)} 字符 → files_rendered.html")

        # ============ 2. 任务详情页附件 ============
        print()
        print("=" * 78)
        print("  【2】任务详情页附件（物理课）")
        print("=" * 78)
        page.goto(f"{config.BASE_URL}/student/classes/{PHYS}/core_tasks")
        page.wait_for_timeout(3000)
        html = page.content()
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        tids = []
        for a in soup.find_all("a", href=True):
            m = re.search(r"/core_tasks/(\d+)", a["href"])
            if m and m.group(1) not in tids:
                tids.append(m.group(1))
        print(f"  任务 {len(tids)} 个")

        found: list[dict] = []
        for tid in tids[:8]:
            u = f"{config.BASE_URL}/student/classes/{PHYS}/core_tasks/{tid}"
            try:
                page.goto(u)
                page.wait_for_timeout(2600)
                t = page.title()
                h = page.content()
            except Exception as e:
                print(f"    [{tid}] 失败 {type(e).__name__}")
                continue

            # 用 JS 找附件链接（更可靠）
            js3 = """
            (() => {
              const out = [];
              document.querySelectorAll('a').forEach(a => {
                const h = a.getAttribute('href') || '';
                const t = (a.innerText || '').trim();
                if (/download|attach|\\.pdf|\\.docx?|\\.pptx?|\\.xlsx?|\\.zip/i.test(h))
                  out.push({h, t: t.slice(0,60)});
              });
              return JSON.stringify(out);
            })()
            """
            links = []
            try:
                raw = page.eval(js3)
                links = json.loads(raw) if raw else []
            except Exception:
                pass

            if links:
                print(f"    [{tid}] «{t[:40]}» → {len(links)} 个附件")
                for l in links[:6]:
                    print(f"        {l['h'][:96]}")
                    if l["t"]:
                        print(f"            «{l['t']}»")
                found.append({"tid": tid, "title": t, "links": links})
            else:
                print(f"    [{tid}] «{t[:40]}» → 无附件")
            if len(found) >= 3:
                break

        (OUT / "task_files.json").write_text(json.dumps(found, ensure_ascii=False, indent=2), encoding="utf-8")

        print()
        print("=" * 78)
        print(f"  产物：{OUT}")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
