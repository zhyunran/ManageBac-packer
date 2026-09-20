"""侦察：作业附件 + Files 页面结构。

目标：
  1. 任务详情页（core_tasks/{tid}）里附件长什么样、下载链接什么形式
  2. 课程 Files 子页面（网盘）的结构：文件 / 文件夹 / 层级

联网执行（需 ManageBac 已登录）。
产物：data/attach_recon/
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bs4 import BeautifulSoup  # noqa: E402

from app import cdp, config  # noqa: E402

OUT = config.DATA_DIR / "attach_recon"
OUT.mkdir(parents=True, exist_ok=True)

# 物理课（用户说有 PDF 附件）
PHYSICS = "11516114"
# IDS Big History（用户说有 Files 子页面）
IDS = "11516047"

HOME = f"{config.BASE_URL}/student/home"


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()


def probe_files_page(page, cid: str, label: str) -> None:
    """探测课程的 Files 子页面（网盘结构）。"""
    print()
    print("=" * 76)
    print(f"  【Files 页面】{label}  ({cid})")
    print("=" * 76)

    candidates = [
        f"/student/classes/{cid}/files",
        f"/student/classes/{cid}/resources",
        f"/student/classes/{cid}/files_and_folders",
        f"/student/classes/{cid}/documents",
    ]
    for path in candidates:
        url = config.BASE_URL + path
        try:
            page.goto(url)
            page.wait_for_timeout(2400)
            title = page.title()
            if "doesn't exist" in title:
                print(f"  [404] {path}")
                continue
            html = page.content()
            fn = OUT / f"files_{cid}{path.split('/')[-1]}.html"
            fn.write_text(html, encoding="utf-8")
            print(f"  [OK ] {path}  {len(html)} 字符  «{title[:44]}»")
            print(f"         → {fn.name}")
            analyze_files(html, path)
            return
        except Exception as e:
            print(f"  [ERR] {path}  {type(e).__name__}")
    print("  （未找到 Files 页面，尝试从课程主页找入口）")

    # 从课程主页找入口
    try:
        page.goto(f"{config.BASE_URL}/student/classes/{cid}")
        page.wait_for_timeout(2400)
        html = page.content()
        soup = BeautifulSoup(html, "html.parser")
        links = set()
        for a in soup.find_all("a", href=True):
            h = a["href"]
            if re.search(r"/(files?|resources?|documents?|materials?)", h, re.I):
                links.add((h, clean(a.get_text(" "))[:30]))
        if links:
            print("  主页里找到这些疑似入口：")
            for h, t in list(links)[:12]:
                print(f"     {h[:70]}  «{t}»")
        else:
            print("  主页里没有明显的 Files 入口")
            # 列出所有导航链接
            navs = set()
            for a in soup.find_all("a", href=True):
                if f"/student/classes/{cid}" in a["href"]:
                    navs.add(a["href"])
            print("  课程内可用链接：")
            for h in sorted(navs)[:20]:
                print(f"     {h}")
    except Exception as e:
        print(f"  主页探测失败：{e}")


def analyze_files(html: str, path: str) -> None:
    """分析 Files 页面的结构。"""
    soup = BeautifulSoup(html, "html.parser")

    print()
    print(f"  ── 分析 {path} ──")

    # 1. 表格
    tables = soup.find_all("table")
    if tables:
        print(f"  表格 {len(tables)} 张")
        for i, tb in enumerate(tables[:3]):
            headers = [clean(th.get_text(" ")) for th in tb.find_all("th")]
            rows = [r for r in tb.find_all("tr") if r.find("td")]
            print(f"    [表{i}] 表头: {headers}")
            print(f"           行数: {len(rows)}")
            for r in rows[:5]:
                cells = [clean(td.get_text(" "))[:34] for td in r.find_all("td")]
                a = r.find("a", href=True)
                print(f"             {cells}  链接={a['href'][:60] if a else ''}")

    # 2. 文件类链接
    print()
    print("  ── 文件/文件夹链接 ──")
    pats = [
        ("download", r"/download|/file_?download"),
        ("files", r"/files?/"),
        ("folders", r"folder"),
        ("s3/assets", r"amazonaws|assets\.managebac"),
        ("attachments", r"attach"),
    ]
    found: dict[str, list[tuple[str, str]]] = {k: [] for k, _ in pats}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = clean(a.get_text(" "))[:40]
        for name, pat in pats:
            if re.search(pat, href, re.I):
                found[name].append((href, text))

    for name, items in found.items():
        if not items:
            continue
        seen = set()
        uniq = []
        for h, t in items:
            if h in seen:
                continue
            seen.add(h)
            uniq.append((h, t))
        print(f"  〔{name}〕{len(uniq)} 个")
        for h, t in uniq[:6]:
            print(f"     {h[:86]}")
            if t:
                print(f"        «{t}»")

    # 3. 重复结构（文件夹/文件行）
    from collections import Counter
    sig: Counter = Counter()
    sample: dict[str, str] = {}
    for el in soup.find_all(["div", "li", "tr", "article"]):
        cls = el.get("class") or []
        if not cls:
            continue
        key = f"{el.name}." + ".".join(sorted(cls))
        t = clean(el.get_text(" "))
        if len(t) < 6:
            continue
        sig[key] += 1
        sample.setdefault(key, t[:120])
    hot = [(k, c) for k, c in sig.most_common(14) if c >= 2]
    if hot:
        print()
        print("  ── 重复结构 ──")
        for k, c in hot:
            print(f"    x{c:<3} {k}")
            print(f"          {sample[k][:110]}")


def probe_task_attachments(page, cid: str, label: str, max_tasks: int = 6) -> None:
    """探测任务详情页里的附件。"""
    print()
    print("=" * 76)
    print(f"  【作业附件】{label}  ({cid})")
    print("=" * 76)

    # 先取任务列表
    try:
        page.goto(f"{config.BASE_URL}/student/classes/{cid}/core_tasks")
        page.wait_for_timeout(2400)
        html = page.content()
    except Exception as e:
        print(f"  取任务列表失败：{e}")
        return

    soup = BeautifulSoup(html, "html.parser")
    tids: list[tuple[str, str]] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        m = re.search(r"/core_tasks/(\d+)", a["href"])
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            tids.append((m.group(1), clean(a.get_text(" "))[:40]))

    print(f"  任务 {len(tids)} 个，逐个检查附件……")
    print()

    all_hits: list[dict] = []
    for tid, tname in tids[:max_tasks]:
        url = f"{config.BASE_URL}/student/classes/{cid}/core_tasks/{tid}"
        try:
            page.goto(url)
            page.wait_for_timeout(2200)
            html = page.content()
        except Exception as e:
            print(f"    [{tid}] 失败 {type(e).__name__}")
            continue

        fn = OUT / f"task_{cid}_{tid}.html"
        fn.write_text(html, encoding="utf-8")

        # 找附件线索
        hits: list[str] = []
        s2 = BeautifulSoup(html, "html.parser")
        for a in s2.find_all("a", href=True):
            h = a["href"]
            if re.search(r"download|attachment|/files?/|\.pdf|\.docx?|\.pptx?|"
                         r"\.xlsx?|\.zip|\.jpg|\.png|amazonaws", h, re.I):
                hits.append(f"{h}  «{clean(a.get_text(' '))[:36]}»")

        # 内嵌 JSON 里的文件线索
        m = re.findall(r'"(?:file_url|download_url|attachment_url|url)"\s*:\s*"([^"]{10,200})"',
                       html)
        for u in m[:8]:
            if re.search(r"\.(pdf|docx?|pptx?|xlsx?|zip|jpg|png)|download|file", u, re.I):
                hits.append(f"[json] {u}")

        if hits:
            print(f"    [{tid}] {tname}  → {len(hits)} 个附件线索")
            for h in hits[:5]:
                print(f"         {h[:120]}")
            all_hits.append({"tid": tid, "name": tname, "hits": hits[:20]})
        else:
            print(f"    [{tid}] {tname}  → 无")

    (OUT / "task_attachments.json").write_text(json.dumps(all_hits, ensure_ascii=False, indent=2), encoding="utf-8")
    if all_hits:
        print()
        print(f"  [OK] {len(all_hits)} 个任务有附件 → task_attachments.json")


def main() -> int:
    config.ensure_dirs()

    if not cdp.is_running(config.BROWSER_PROFILE_DIR):
        print("[i] 启动 ManageBac 浏览器……")
        try:
            cdp.launch(config.BROWSER_PROFILE_DIR, headless=True, url=HOME)
            cdp.wait_for_port(config.BROWSER_PROFILE_DIR, timeout=45)
        except Exception as e:
            print(f"[X] {e}")
            return 2

    session = cdp.Session(headless=True)
    session.open()
    try:
        page = session.page()
        page.goto(HOME)
        page.wait_for_timeout(1800)
        if not cdp.looks_logged_in(page):
            print("[X] 未登录，请先运行：uv run connect.py")
            return 2

        # 1. Files 页面
        probe_files_page(page, IDS, "IDS Big History")
        probe_files_page(page, PHYSICS, "Physics10")

        # 2. 任务附件
        probe_task_attachments(page, PHYSICS, "Physics10")

        print()
        print("=" * 76)
        print(f"  产物目录：{OUT}")
        print("=" * 76)
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
