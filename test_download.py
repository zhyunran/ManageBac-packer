"""实测：下载物理作业 PDF 与 IDS 的 PPT，验证下载模块。"""
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import cdp, config, download
from bs4 import BeautifulSoup

PHYS = "11516114"

print("=" * 76)
print("  下载功能实测")
print("=" * 76)

# ---- 1. 从任务详情页收集附件真实链接 ----
session = cdp.Session(headless=True)
session.open()
items = []
try:
    page = session.page()
    page.goto(f"{config.BASE_URL}/student/home")
    page.wait_for_timeout(1200)

    page.goto(f"{config.BASE_URL}/student/classes/{PHYS}/core_tasks")
    page.wait_for_timeout(2800)
    html = page.content()
    soup = BeautifulSoup(html, "html.parser")
    tids = []
    for a in soup.find_all("a", href=True):
        import re
        m = re.search(r"/core_tasks/(\d+)", a["href"])
        if m and m.group(1) not in tids:
            tids.append(m.group(1))
    print(f"\n物理课任务 {len(tids)} 个，找附件……")

    js = """
    (() => {
      const out = [];
      document.querySelectorAll('a').forEach(a => {
        const h = a.getAttribute('href') || '';
        const t = (a.innerText || '').trim();
        if (/\\/attachments\\//i.test(h))
          out.push({url: h, text: t.slice(0,80)});
      });
      return JSON.stringify(out);
    })()
    """
    import json
    for tid in tids[:8]:
        try:
            page.goto(f"{config.BASE_URL}/student/classes/{PHYS}/core_tasks/{tid}")
            page.wait_for_timeout(2400)
            raw = page.eval(js)
            found = json.loads(raw) if raw else []
            if found:
                print(f"  [{tid}] 找到 {len(found)} 个附件")
                for f in found:
                    u = f["url"]
                    if u.startswith("/"):
                        u = config.BASE_URL + u
                    name = download.guess_name(u, f["text"])
                    print(f"      {name}")
                    items.append({"url": u, "name": name, "text": f["text"]})
        except Exception as e:
            print(f"  [{tid}] 失败 {type(e).__name__}")
        if len(items) >= 2:
            break
finally:
    session.close()

if not items:
    print("\n[X] 没有找到可下载的附件")
    sys.exit(1)

# ---- 2. 下载 ----
print()
print(f"开始下载 {len(items)} 个文件到：{download.download_root()}")
print()
results = download.download_all(items, subdir="物理作业")

print()
print("=" * 76)
ok = 0
for r in results:
    if r["ok"]:
        ok += 1
        size = r["size"]
        unit = f"{size/1024:.1f} KB" if size > 1024 else f"{size} B"
        print(f"  [OK] {r['name']}  ({unit})")
        print(f"       → {r['path']}")
    else:
        print(f"  [X ] {r['name']}  {r.get('error')}")
print()
print(f"  成功 {ok} / {len(results)}")
print("=" * 76)
