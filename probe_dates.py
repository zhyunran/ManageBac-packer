"""取证：① 每任务的提交状态在哪  ② JAN 17 日期为何没抓到。

联网执行（需已登录）。产物：
    data/probe_out/*.html      页面快照
    data/probe_out/report.txt  分析报告
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bs4 import BeautifulSoup  # noqa: E402

from app import cdp, config  # noqa: E402

OUT = config.DATA_DIR / "probe_out"
OUT.mkdir(parents=True, exist_ok=True)
lines: list[str] = []


def L(s: str = "") -> None:
    lines.append(str(s))


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()


def ctx(text: str, i: int, before: int = 150, after: int = 220) -> str:
    return re.sub(r"\s+", " ", text[max(0, i - before):i + after])


CHEM = ("11516088", "Chemistry")
CHINESE = ("11512069", "Chinese")


def probe_course(page, cid: str, label: str) -> None:
    L()
    L("#" * 74)
    L(f"#  {label}  ({cid})")
    L("#" * 74)

    for tag, path in [
        ("class", f"/student/classes/{cid}"),
        ("coretasks", f"/student/classes/{cid}/core_tasks"),
    ]:
        url = config.BASE_URL + path
        try:
            page.goto(url)
            page.wait_for_timeout(2600)
            html = page.content()
        except Exception as e:
            L(f"  [{tag}] 访问失败 {type(e).__name__}: {e}")
            continue

        fn = OUT / f"{label}_{tag}.html"
        fn.write_text(html, encoding="utf-8")
        L()
        L(f"── [{tag}] {path}  ({len(html)} 字符) → {fn.name}")

        # ---- 关键词扫描 ----
        for kw, pat in [
            ("submitted", r"submitted"),
            ("not submitted", r"not\s+submitted"),
            ("unsubmitted", r"unsubmitted"),
            ("due/overdue", r"\boverdue\b"),
            ("Jan", r"Jan(?:uary)?\.?\s*\d{1,2}"),
            ("check icon", r"fi-?check|fa-?check|check-circle|checkmark"),
        ]:
            hits = list(re.finditer(pat, html, re.I))
            if hits:
                L(f"    [{kw}] 命中 {len(hits)} 处")
                seen = set()
                for m in hits[:4]:
                    c = ctx(html, m.start())[:230]
                    k = c[:70]
                    if k in seen:
                        continue
                    seen.add(k)
                    L(f"       · {c}")

        # ---- 任务卡片逐一分析 ----
        soup = BeautifulSoup(html, "html.parser")
        seen_ids: set[str] = set()
        L()
        L("    · 任务卡片明细：")
        for a in soup.find_all("a", href=True):
            m = re.search(r"/core_tasks/(\d+)", a["href"])
            if not m or m.group(1) in seen_ids:
                continue
            tid = m.group(1)
            seen_ids.add(tid)

            in_cal = bool(a.find_parent("table", class_=lambda c: c and "calendar" in " ".join(c)))

            cont = a
            if not in_cal:
                for _ in range(7):
                    p = cont.parent
                    if p is None or p.name in ("body", "html"):
                        break
                    cont = p
                    if cont.select_one(".assessment-cell") or cont.select_one(".date-badge"):
                        break

            title = clean(a.get_text(" "))
            cardtext = clean(cont.get_text(" "))[:200]
            badge = cont.select_one(".date-badge")
            badge_txt = clean(badge.get_text(" ")) if badge else ""
            due = cont.select_one(".due")
            due_txt = clean(due.get_text(" ")) if due else ""
            L(f"      [{tid}] cal={in_cal}  badge={badge_txt!r}  due={due_txt!r}")
            L(f"             title={title[:56]!r}")
            L(f"             text={cardtext[:150]}")

        if not seen_ids:
            L("      （未发现 core_tasks 链接）")


def probe_task_detail(page, cid: str, tid: str, label: str) -> None:
    url = f"{config.BASE_URL}/student/classes/{cid}/core_tasks/{tid}"
    try:
        page.goto(url)
        page.wait_for_timeout(2600)
        html = page.content()
    except Exception as e:
        L(f"  详情页访问失败：{e}")
        return
    fn = OUT / f"{label}_task_{tid}.html"
    fn.write_text(html, encoding="utf-8")
    L()
    L(f"── 任务详情 {tid}  ({len(html)} 字符) → {fn.name}")
    for kw, pat in [("submitted", r"submitted"),
                    ("file/download", r"download|attachment|files?/"),
                    ("Jan", r"Jan(?:uary)?\.?\s*\d{1,2}")]:
        hits = list(re.finditer(pat, html, re.I))
        L(f"    [{kw}] 命中 {len(hits)} 处")
        seen = set()
        for m in hits[:5]:
            c = ctx(html, m.start())[:220]
            k = c[:70]
            if k in seen:
                continue
            seen.add(k)
            L(f"       · {c}")


def main() -> int:
    config.ensure_dirs()

    if not cdp.is_running(config.BROWSER_PROFILE_DIR):
        print("[i] 浏览器未运行，尝试启动……")
        try:
            cdp.launch(config.BROWSER_PROFILE_DIR, headless=True,
                       url=f"{config.BASE_URL}/student/home")
            cdp.wait_for_port(config.BROWSER_PROFILE_DIR, timeout=45)
        except Exception as e:
            print(f"[X] 启动失败：{e}")
            return 2

    session = cdp.Session(headless=True)
    session.open()
    try:
        page = session.page()
        page.goto(f"{config.BASE_URL}/student/home")
        page.wait_for_timeout(1800)
        if not cdp.looks_logged_in(page):
            print("[X] 未登录，请先运行 uv run connect.py")
            return 2

        for cid, label in (CHEM, CHINESE):
            probe_course(page, cid, label)

        # 化学海报任务的详情页（先找到它的 ID）
        chem_html = OUT / "Chemistry_coretasks.html"
        if chem_html.exists():
            soup = BeautifulSoup(chem_html.read_text(encoding="utf-8"), "html.parser")
            target = None
            for a in soup.find_all("a", href=True):
                m = re.search(r"/core_tasks/(\d+)", a["href"])
                if not m:
                    continue
                txt = clean(a.get_text(" "))
                if "海报" in txt or "poster" in txt.lower() or "化学" in txt:
                    target = m.group(1)
                    break
            if target is None:
                # 退路：取第一个
                for a in soup.find_all("a", href=True):
                    m = re.search(r"/core_tasks/(\d+)", a["href"])
                    if m:
                        target = m.group(1)
                        break
            if target:
                L()
                L(f"[!] 选中化学任务 {target} 作为详情页样例")
                probe_task_detail(page, CHEM[0], target, "Chemistry")

        report = OUT / "report.txt"
        report.write_text("\n".join(lines), encoding="utf-8")
        print(f"[OK] 报告：{report}")
        print(f"     HTML：{OUT}")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
