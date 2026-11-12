"""EC 来源专项验证：确认名单只从 PDF 附件取，不碰消息正文。

★ 为什么要有这个测试
  EC 的名单在**附件 PDF** 里，正文只有场地/改期通知。
  实测过的真实正文：
      「There will be NO EC tomorrow. English Immersion remains in full effect…」
      「TODAY'S EC will be outside at the ID courtyard under the stress…」
  —— 一个名字都没有。

  如果哪天有人「顺手」把正文也当名单源扫了，用户会看到
  「名单里没你」这种误导性结论（其实是正文里本来就没有名字）。
  这个测试就是盯着这条线。

造一份数据：正文里放「Someone Else」，
PDF 文字（file_text）里放「Test Name」，然后只关注 Test Name，
应该命中；再关注 Someone Else，应该**不命中**。
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config, ec_watch  # noqa: E402

bad = 0


def chk(ok: bool, label: str, extra: str = "") -> None:
    global bad
    print(f"  {'[OK]' if ok else '[X] '} {label}" + (f"  {extra}" if extra else ""))
    if not ok:
        bad += 1


def main() -> int:
    print("=" * 68)
    print("  EC 来源验证：名单只来自 PDF 附件")
    print("=" * 68)

    crawl = config.DATA_DIR / "teams_crawl.json"
    backup = config.DATA_DIR / "_crawl_backup_test.json"
    had = crawl.exists()
    if had:
        shutil.copy2(crawl, backup)

    fake = {
        "ok": True, "partial": False,
        "teams": [{
            "name": "Test School",
            "id": "t1",
            "channels": [{
                "name": "ENGLISH CORNER ROSTER",
                "id": "c1",
                "posts": [
                    {
                        #  正文里有名字，但它是正文 —— 不该被当名单
                        "title": "EC ANNOUNCEMENT",
                        "text": "There will be NO EC tomorrow. "
                                "Contact BodyOnly Person for details.",
                        "time": "2026-09-25 09:00",
                        #  PDF 里才是真名单
                        "file_text": "ENGLISH CORNER ROSTER\n"
                                     "The following students must report:\n"
                                     "Test Name (14)  Other Student (3)\n"
                                     "Time: 10:00-10:40  Location: E107\n",
                        "attachments": [
                            {"name": "EC Roster 9.25.pdf",
                             "url": "https://x.sharepoint.com/sites/a/b.pdf"},
                        ],
                    },
                ],
            }],
        }],
        "at": "2026-09-25 09:00:00",
    }
    config.ensure_dirs()
    crawl.write_text(json.dumps(fake, ensure_ascii=False), encoding="utf-8")

    try:
        srcs = ec_watch._ec_sources()
        print(f"\n  可扫来源：{len(srcs)} 份")
        for s in srcs:
            print(f"    · {s.get('file')}  via={s.get('via')}"
                  f"  {len(s.get('text') or '')} 字")

        chk(len(srcs) == 1, "只收 1 份来源（PDF 那份）")
        if srcs:
            chk(srcs[0].get("via") == "pdf", "来源标记为 pdf")
            t = srcs[0].get("text") or ""
            chk("Test Name" in t, "来源文字里有 PDF 内容")
            chk("BodyOnly Person" not in t,
                "正文没被混进来（BodyOnly Person 不在）")

        #  ── 扫描：关注 PDF 里的名字，应命中 ──
        print("\n  ── 扫「Test Name」（在 PDF 里）──")
        r = ec_watch.scan_recent(names=["Test Name"], hours=24 * 30)
        print(f"    scanned={r.get('scanned')} found={r.get('found')}")
        chk("Test Name" in (r.get("found") or []), "命中 PDF 里的名字")
        chk(bool(r.get("details")), "给出命中片段")
        if r.get("details"):
            d = r["details"][0]
            chk("Test Name" in (d.get("text") or ""), "片段的原文里有那个名字")
            chk("EC Roster" in (d.get("file") or ""),
                "来源显示的是 PDF 文件名（不是消息标题）",
                f"→ {d.get('file')}")
            sig = d.get("signals") or {}
            chk(bool(sig.get("times")), "认出时间", f"→ {sig.get('times')}")
            chk(bool(sig.get("places")), "认出地点", f"→ {sig.get('places')}")

        #  ── 扫描：关注只出现在正文里的名字，不该命中 ──
        print("\n  ── 扫「BodyOnly Person」（只在正文里）──")
        r2 = ec_watch.scan_recent(names=["BodyOnly Person"], hours=24 * 30)
        print(f"    scanned={r2.get('scanned')} found={r2.get('found')}")
        chk("BodyOnly Person" not in (r2.get("found") or []),
            "正文里的名字不被当名单命中")

        #  ── 语义声明 ──
        print("\n  ── 语义声明 ──")
        chk(r.get("text_only") is True, "标了 text_only")
        chk(r.get("semantic_verified") is False, "标了 semantic_verified=false")
        chk(bool(r.get("note")), "带了边界说明", f"→ {r.get('note')}")
    finally:
        if had:
            shutil.copy2(backup, crawl)
            backup.unlink(missing_ok=True)
        else:
            crawl.unlink(missing_ok=True)

    print("\n" + "=" * 68)
    print("  [OK] 全部通过" if not bad else f"  [X] {bad} 项没通过")
    print("=" * 68)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
