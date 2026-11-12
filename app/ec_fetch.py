"""EC 名单抓取 —— 把 Teams 频道里的 PDF 下下来、抽出文字、存好。

【完整链路（每一段都是实测验证过的）】

    ① 进 EC 频道
         Teams → Beijing 101 High School → ENGLISH CORNER ROSTER
         深度链接 `_#/l/channel/{id}` 直达，不用点。

    ② 读附件卡
         附件卡懒挂载：光滚进视口不够，得**真的绘制一次**。
         → `teams_crawl.collect_attachments()`（内含逼绘制）

    ③ 下载 PDF
         ✗ 纯 HTTP 直连 → 26 KB 的 HTML「Redirecting」
            （SharePoint 登录态不在 ManageBac 的 cookie 里）
         ✓ 导航到分享链接 → 预览页 → **页面内 fetch** 取字节
            → `sp_download.fetch_to_file()`

    ④ 抽文字
         `teams.extract_pdf_text()`（pypdf）。
         实测 EC 名单是**真文字**不是扫描件，3648 字一次读全。

    ⑤ 存
         文字挂回 `data/teams_crawl.json` 的
         `teams[].channels[].posts[].file_text`，
         然后 `ec_watch._ec_sources()` 就能扫到了。

【为什么不做成「后台自动跑」】

    Teams 要用户先登录一次，这一步绕不过去。
    所以设计成「你点一下」而不是「偷偷跑」——
    和 teams_reader 的同步按钮同一个思路。

【怎么找 EC 频道】

    不写死频道 id（会变）。按名字找：
      团队名含 `Beijing 101`，频道名含 `ENGLISH CORNER` / `EC`。
    找不到就如实说找不到，不猜。
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from pathlib import Path

#  直接 `python app/ec_fetch.py` 跑自测时，包上下文不在
#  （相对导入会报 "attempted relative import with no known parent package"）。
#  把项目根塞进 sys.path，然后按顶层包导入 —— 两种跑法都能用。
if __package__ in (None, ""):
    _root = str(Path(__file__).resolve().parent.parent)
    if _root not in sys.path:
        sys.path.insert(0, _root)

from app import config, sp_download, teams, teams_crawl  # noqa: E402

log = logging.getLogger("ec_fetch")

#  EC 频道的识别词（宽松一些，Teams 改过名）
TEAM_HINTS = ("beijing 101", "101 high", "101 school", "北京101")
CHAN_HINTS = ("english corner", "ec roster", "ec list", "english  corner")

#  一次最多下几份（名单文件不大，但别一次拉太多）
MAX_PDFS = 8
#  单份 PDF 抽文字后保留多少字
MAX_TEXT = 20000


def find_channel() -> dict:
    """在已抓取的缓存里找 EC 频道。

    返回 ``{ok, team, channel, channel_id, posts, note?}``。

    ★ 从缓存找，不再去浏览器里翻一遍 —— 用户点同步的时候已经翻过了。
      缓存里没有就说没有（并提示去同步），而不是自己去开浏览器。
    """
    try:
        data = teams_crawl.load() or {}
    except Exception as e:
        return {"ok": False, "note": f"读缓存失败：{e}"}

    if not (data.get("teams") or []):
        return {"ok": False, "note": "还没有 Teams 数据，先点一次「同步」"}

    cands: list[dict] = []
    for t in (data.get("teams") or []):
        tname = (t.get("name") or "").strip()
        tl = tname.lower()
        for ch in (t.get("channels") or []):
            cname = (ch.get("name") or "").strip()
            cl = cname.lower()

            #  打分：团队名 + 频道名各占一部分
            score = 0
            if any(h in tl for h in TEAM_HINTS):
                score += 2
            if any(h in cl for h in CHAN_HINTS):
                score += 3
            #  「EC」单独一词也算（但要避免匹配到 ACCESS 之类）
            if re.search(r"(?<![a-z])ec(?![a-z])", cl):
                score += 2
            if score <= 0:
                continue
            n_at = sum(len(p.get("attachments") or [])
                       for p in (ch.get("posts") or []))
            cands.append({
                "team": tname, "channel": cname,
                "channel_id": ch.get("id") or "",
                "posts": ch.get("posts") or [],
                "nAttach": n_at, "score": score,
            })

    if not cands:
        return {"ok": False,
                "note": "缓存里没找到 EC 频道（名字里含 EC / English Corner "
                        "的频道）。先点一次「同步」把频道抓下来。"}

    #  同名分高者优先；同分则附件多的优先（更有可能是名单频道）
    cands.sort(key=lambda x: (-x["score"], -x["nAttach"]))
    best = cands[0]
    best["ok"] = True
    if len(cands) > 1:
        best["others"] = [f"{c['team']} / {c['channel']}" for c in cands[1:5]]
    return best


def _pdf_targets(posts: list[dict]) -> list[dict]:
    """从消息里挑出要下的 PDF（去重，按消息时间倒序）。

    ★ 顺手把「站点库路径」带上（`site_path`）——
      站点库链接（`/sites/…`）会跳 MFA 下不了，
      这时要靠这个路径里的**目录名**去个人盘找同名文件。
    """
    out: list[dict] = []
    seen: set = set()
    for p in posts:
        if not isinstance(p, dict):
            continue
        for a in (p.get("attachments") or []):
            url = (a.get("url") or "").strip()
            name = (a.get("name") or "").strip()
            if not url:
                continue
            #  只下 PDF —— 其它类型对名单没用
            if not (sp_download.is_pdf_name(name)
                    or sp_download.is_pdf_name(url)):
                continue
            if url in seen:
                continue
            seen.add(url)
            #  站点库的路径（用来推个人盘目录）
            site_path = ""
            m = re.search(r"https?://[^/]+(/sites/[^\s]+)", url)
            if m:
                from urllib.parse import unquote as _uq
                site_path = _uq(m.group(1))
            out.append({"url": url, "name": name or "EC Roster.pdf",
                        "post": p, "site_path": site_path})
    return out


def _safe_name(name: str, fallback: str = "EC Roster.pdf") -> str:
    n = re.sub(r'[\\/:*?"<>|]+', "_", str(name or "")).strip()
    n = re.sub(r"\s+", " ", n)[:150]
    if not n:
        n = fallback
    if not sp_download.is_pdf_name(n):
        n += ".pdf"
    return n


def download_and_extract(*, verbose: bool = False,
                         progress=None) -> dict:
    """下载 EC 频道里的 PDF 并抽出文字，存回缓存。

    返回 ``{ok, channel, downloaded, extracted, pdfs[], note?}``。
    """
    def say(msg: str) -> None:
        log.info(msg)
        if progress:
            try:
                progress(msg)
            except Exception:
                pass

    loc = find_channel()
    if not loc.get("ok"):
        return {"ok": False, "note": loc.get("note") or "找不到 EC 频道",
                "downloaded": 0, "extracted": 0, "pdfs": []}

    posts = loc.get("posts") or []
    targets = _pdf_targets(posts)
    say(f"EC 频道：{loc['team']} / {loc['channel']}"
        f"（{len(posts)} 条消息，{len(targets)} 份 PDF）")

    if not targets:
        #  没有 PDF 的时候要说清「为什么」——
        #  最常见的原因是消息同步时附件卡没挂载（没逼绘制）
        n_grid_hint = any(
            "附件" in str(p.get("text") or "") for p in posts)
        note = ("这个频道里没找到 PDF 附件。"
                "若确实有名单，多半是同步时附件卡没挂载 —— "
                "重跑一次「同步」（同步过程会自动逼绘制）")
        if n_grid_hint:
            note = "消息里提到附件但没读到文件名，重跑一次同步试试"
        return {"ok": False, "note": note,
                "channel": f"{loc['team']} / {loc['channel']}",
                "downloaded": 0, "extracted": 0, "pdfs": []}

    #  ── 开浏览器（复用 profile 里的 SharePoint 登录态）──
    from app import teams_reader
    say("正在打开 Teams 窗口…")
    if not teams_reader.open_teams(headless=False):
        return {"ok": False, "note": "打不开 Teams 窗口",
                "downloaded": 0, "extracted": 0, "pdfs": []}
    teams_reader.wait_teams_ready(timeout=120, quiet=True)

    page = None
    for _ in range(10):
        try:
            page = teams_reader._get_page(wait_ready_sec=40)  # noqa: SLF001
        except Exception:
            page = None
        if page:
            break
        time.sleep(4)
    if not page:
        return {"ok": False, "note": "拿不到浏览器页面",
                "downloaded": 0, "extracted": 0, "pdfs": []}

    out_dir = Path(teams.EC_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)

    done: list[dict] = []
    n_dl = n_txt = 0
    need_login: list[str] = []          # 需要用户手动过一次 MFA 的

    for i, tg in enumerate(targets[:MAX_PDFS], 1):
        name = _safe_name(tg["name"])
        say(f"[{i}/{min(len(targets), MAX_PDFS)}] {name}")

        #  已经下过且抽过文字 → 跳过（省得每次都重新拉几 MB）
        #
        #  ★ 两个来源都看，缺一不可：
        #    ① json 里挂着的 file_text（同一次运行里刚抽的）
        #    ② **本地目录里的 PDF 本体**（上次运行留下的）
        #    只看 ① 的话，每次重跑都会把几 MB 重新下一遍。
        cached = _cached_text(tg["url"])
        local_pdf = out_dir / name
        if not cached and local_pdf.exists() and local_pdf.stat().st_size > 5000:
            try:
                ex0 = teams.extract_pdf_text(local_pdf)
                if ex0.get("ok"):
                    cached = ex0.get("text") or ""
            except Exception:
                pass
        if cached:
            tg["post"]["file_text"] = cached[:MAX_TEXT]
            tg["post"]["file_note"] = "已缓存"
            n_txt += 1
            done.append({"name": name, "ok": True, "cached": True,
                         "chars": len(cached)})
            if verbose:
                print(f"      已缓存（{len(cached)} 字）")
            continue

        r = sp_download.fetch_document(page, tg["url"],
                                       verbose=verbose)
        if not r.get("ok"):
            note = r.get("note") or "下载失败"
            #  站点库要过 MFA —— 这种情况要**说清楚**，
            #  而且**不能**说「重试就行」（实测：不做完安全设置就一直不行）。
            if r.get("kind") == "need_login":
                mfa = r.get("host") == "login.microsoftonline.com"
                note = ("这份在站点文档库里，这个链接需要 Microsoft 的"
                        "二次验证（MFA）。实测：在浏览器里打开一次"
                        "该链接并**完成安全信息设置**后就能下；"
                        "没设置完之前，重试也不会通"
                        if mfa else
                        f"需要先登录 {r.get('host') or 'SharePoint'}"
                        "（在浏览器里打开一次那个链接即可）")
            say(f"      下载失败：{note}")
            if r.get("kind") == "need_login":
                need_login.append(name)
            done.append({"name": name, "ok": False, "note": note,
                         "kind": r.get("kind") or ""})
            continue
        n_dl += 1
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / name
        try:
            path.write_bytes(r["data"])
        except Exception as e:
            done.append({"name": name, "ok": False,
                         "note": f"写文件失败：{e}"})
            continue
        via = r.get("via") or "share"
        say(f"      已下载 {r.get('size', 0) / 1024:.0f} KB"
            f"{'（走个人盘）' if via == 'personal' else ''}")

        #  ── 抽文字 ──
        ex = teams.extract_pdf_text(path)
        if ex.get("ok") and (ex.get("text") or "").strip():
            txt = ex["text"]
            n_txt += 1
            tg["post"]["file_text"] = txt[:MAX_TEXT]
            tg["post"]["file_note"] = ex.get("note") or ""
            #  顺手落一个文字旁文件 —— 这样 `ec_watch` 的
            #  「读本地目录」那条路也能扫到，不依赖 json。
            #  ★ 原子写：写一半被杀会留下截断的 .txt，
            #    而 ec_watch 会把「读不出来」当成「这份名单没有名字」，
            #    表现成「明明在名单上却查不到」。
            try:
                _p = out_dir / (path.stem + ".txt")
                _tmp = _p.with_name(_p.name + f".tmp.{os.getpid()}")
                _tmp.write_text(txt, encoding="utf-8")
                os.replace(_tmp, _p)
            except Exception as e:
                #  以前是 pass —— 旁文件写不出来就永远不知道
                log.warning("写名单文字旁文件失败（%s）：%s", path.stem, e)
            done.append({"name": name, "ok": True, "chars": len(txt),
                         "size": r.get("size", 0)})
            say(f"      抽到 {len(txt)} 字")
        else:
            done.append({"name": name, "ok": False,
                         "note": ex.get("note") or "抽不出文字"})
            say(f"      抽文字失败：{ex.get('note')}")

    #  ── 存回缓存 ──
    try:
        data = teams_crawl.load() or {}
        #  把改过的帖子写回去（按 channel_id 定位）
        cid = loc.get("channel_id") or ""
        for t in (data.get("teams") or []):
            for ch in (t.get("channels") or []):
                if cid and ch.get("id") != cid:
                    continue
                #  用 posts 的键做合并，把 file_text 搬过去
                old = ch.get("posts") or []
                bykey = {teams_crawl._msg_key(p): p            # noqa: SLF001
                         for p in old if isinstance(p, dict)}
                for p in posts:
                    k = teams_crawl._msg_key(p)                # noqa: SLF001
                    if k in bykey:
                        if p.get("file_text"):
                            bykey[k]["file_text"] = p["file_text"]
                            bykey[k]["file_note"] = p.get("file_note") or ""
                        #  附件列表也补上（顺手把链接存下来）
                        if p.get("attachments"):
                            bykey[k].setdefault("attachments", [])
                            have = {(a.get("url"), a.get("name"))
                                    for a in bykey[k]["attachments"]}
                            for a in p["attachments"]:
                                if (a.get("url"), a.get("name")) not in have:
                                    bykey[k]["attachments"].append(a)
                ch["posts"] = old
        teams_crawl.save(data)
        say("已写入缓存")
    except Exception as e:
        log.warning("写回缓存失败：%s", e)
        return {"ok": True, "note": f"下载完成但写缓存失败：{e}",
                "channel": f"{loc['team']} / {loc['channel']}",
                "downloaded": n_dl, "extracted": n_txt, "pdfs": done}

    return {
        "ok": True,
        "channel": f"{loc['team']} / {loc['channel']}",
        "downloaded": n_dl,
        "extracted": n_txt,
        "total": len(targets),
        "pdfs": done,
        "dir": str(out_dir),
        "need_login": need_login,
        "note": _note_for(n_txt, need_login, done),
    }


def _note_for(n_txt: int, need_login: list[str], done: list[dict]) -> str:
    """给这次下载写一句人话总结。"""
    if need_login:
        return (f"有 {len(need_login)} 份在站点文档库里，"
                f"需要先完成一次 Microsoft 二次验证（MFA）："
                f"{'、'.join(need_login[:3])}"
                + (" 等" if len(need_login) > 3 else ""))
    if not n_txt and done:
        failed = [d for d in done if not d.get("ok")]
        if failed and all(not d.get("ok") for d in done):
            return failed[0].get("note") or "这份都没有下下来"
        return "下载了 PDF 但没能抽出文字"
    return ""


def _cached_text(url: str) -> str:
    """这条链接的文字是不是已经抽过并存下来了（按 URL 找）。"""
    try:
        data = teams_crawl.load() or {}
    except Exception:
        return ""
    for t in (data.get("teams") or []):
        for ch in (t.get("channels") or []):
            for p in (ch.get("posts") or []):
                if not isinstance(p, dict):
                    continue
                txt = p.get("file_text") or ""
                if not str(txt).strip():
                    continue
                for a in (p.get("attachments") or []):
                    if (a.get("url") or "") == url:
                        return str(txt)
    return ""


def latest_roster_text() -> dict:
    """拿「最新一份 EC 名单的文字」。返回 {ok, text, name, at, note?}。

    ★ 从已下载的 PDF 里现场抽 —— 不依赖中间产物。
      按文件名里的日期挑最新的；挑不出日期就看下载时间。
    """
    d = Path(teams.EC_DIR)
    if not d.exists():
        return {"ok": False, "note": "还没下载过 EC 名单",
                "text": "", "name": "", "at": ""}

    pdfs = sorted(d.glob("*.pdf"))
    if not pdfs:
        return {"ok": False, "note": "EC 目录里没有 PDF",
                "text": "", "name": "", "at": ""}

    def rank(p: Path) -> tuple:
        #  文件名里的 月.日 优先（EC Roster 9.18.pdf）
        m = re.search(r"(\d{1,2})[._](\d{1,2})", p.name)
        key = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
        try:
            mt = p.stat().st_mtime
        except Exception:
            mt = 0
        return (key, mt)

    pdfs.sort(key=rank, reverse=True)
    for p in pdfs:
        try:
            ex = teams.extract_pdf_text(p)
        except Exception as e:
            log.debug("抽 %s 失败：%s", p.name, e)
            continue
        if ex.get("ok") and (ex.get("text") or "").strip():
            return {"ok": True, "text": ex["text"], "name": p.name,
                    "at": time.strftime("%Y-%m-%d %H:%M:%S",
                                        time.localtime(p.stat().st_mtime)),
                    "note": ex.get("note") or ""}
    return {"ok": False, "note": "EC 目录里的 PDF 都抽不出文字（可能是扫描件）",
            "text": "", "name": "", "at": ""}


def _self_test() -> int:
    """自测：只测纯逻辑，不碰浏览器、不联网。"""
    ok = fail = 0

    def chk(cond, label):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  [OK] {label}")
        else:
            fail += 1
            print(f"  [X ] {label}")

    print("=" * 62)
    print("  EC 抓取模块自测（离线）")
    print("=" * 62)

    #  ── 频道识别 ──
    fake = {"teams": [
        {"name": "Beijing 101 High School", "channels": [
            {"id": "c1", "name": "ENGLISH CORNER ROSTER", "posts": [
                {"attachments": [{"name": "EC Roster 9.18.pdf", "url": "u1"}],
                 "author": "a", "time": "9/18 8:00", "text": "x"}]},
            {"id": "c2", "name": "General", "posts": []},
        ]},
        {"name": "Another Team", "channels": [
            {"id": "c3", "name": "EC", "posts": []}]},
    ]}
    saved = teams_crawl.load
    teams_crawl.load = lambda: json.loads(json.dumps(fake))
    try:
        loc = find_channel()
        chk(loc.get("ok"), "能认出 EC 频道")
        chk(loc.get("channel") == "ENGLISH CORNER ROSTER",
            "挑中的是 ROSTER 那个（分更高）")
        chk(loc.get("channel_id") == "c1", "频道 id 对")
        chk(loc.get("nAttach") == 1, "数出 1 个附件")

        #  没有任何线索时要**如实说找不到**
        teams_crawl.load = lambda: {"teams": [
            {"name": "X", "channels": [{"id": "z", "name": "Random"}]}]}
        chk(not find_channel().get("ok"), "没线索 → 报告找不到")

        #  完全没数据
        teams_crawl.load = lambda: {}
        r = find_channel()
        chk(not r.get("ok") and "同步" in (r.get("note") or ""),
            "没数据 → 提示去同步")
    finally:
        teams_crawl.load = saved

    #  ── 挑 PDF ──
    posts = [
        {"attachments": [{"name": "EC Roster 9.18.pdf", "url": "u1"},
                         {"name": "notice.docx", "url": "u2"}]},
        {"attachments": [{"name": "EC Roster 9.18.pdf", "url": "u1"},
                         {"name": "EC Roster 9.17.pdf", "url": "u3"}]},
        {"attachments": [{"name": "", "url": "https://x/a.pdf"}]},
        {},
    ]
    tg = _pdf_targets(posts)
    chk(len(tg) == 3, f"挑出 3 份 PDF（实际 {len(tg)}）")
    chk(all(sp_download.is_pdf_name(t["name"]) for t in tg),
        "全是 PDF（docx 被排除）")
    chk(len({t["url"] for t in tg}) == 3, "URL 去重")

    #  非 PDF 要全排除
    chk(_pdf_targets([{"attachments": [
        {"name": "a.png", "url": "u"}, {"name": "b.docx", "url": "v"}]}]) == [],
        "非 PDF 一个都不收")

    #  ── 文件名清洗 ──
    chk(_safe_name("EC Roster 9.18.pdf") == "EC Roster 9.18.pdf", "正常名字不变")
    chk("/" not in _safe_name("a/b.pdf"), "路径分隔符被替换")
    chk(_safe_name("") == "EC Roster.pdf", "空名字有兜底")
    chk(_safe_name("file") == "file.pdf", "没扩展名自动补 .pdf")
    chk(len(_safe_name("x" * 400)) <= 154, "超长名字被截断")

    print("-" * 62)
    print(f"  通过 {ok} / 失败 {fail}")
    return 1 if fail else 0


if __name__ == "__main__":
    import sys
    from pathlib import Path as _P
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    #  直接 `python app/ec_fetch.py` 跑时，包上下文不在，
    #  把项目根塞进 sys.path 再按顶层包导入
    _root = str(_P(__file__).resolve().parent.parent)
    if _root not in sys.path:
        sys.path.insert(0, _root)
    from app.ec_fetch import _self_test as _st
    raise SystemExit(_st())
