"""课程资料（Files）—— 像网盘一样浏览与下载。

【实测结构】（纯 HTTP，无需浏览器）
    根目录 : /student/classes/{cid}/files
    子文件夹: /student/classes/{cid}/files/folder/{fid}
    分类    : /student/classes/{cid}/files/category/{name}

    每行都是一个 `div.row.file`：
      文件夹（class 含 `mt-0`）:
        <div class="row file mt-0 px-4">
          <a href="/student/classes/{cid}/files/folder/{fid}">
            <div class="hstack">Origin Stories Project</div>
          </a>
        </div>
      文件:
        <div class="row file px-4">
          <a class="title-link" href="https://...s3.../xxx.pptx">
            <div class="details">Introduction_Class_-_IDS10.pptx by Mike Joyce</div>
            <div class="size">5 MB</div>
          </a>
        </div>

【保存位置】桌面/ManageBac 资料/<课程名>/<文件夹路径>/<文件名>

【实测结果（IDS 课程）】
    根目录 2 个文件夹 + 1 个文件
    递归共 2 夹 / 13 文件
"""
from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path

from bs4 import BeautifulSoup

from . import config, httpclient

log = logging.getLogger("files")

# 缓存（避免每次切栏目都重新抓）
_CACHE_TTL = 180          # 秒
_cache: dict[str, tuple[float, dict]] = {}
_lock = threading.RLock()


# ---------------------------------------------------------------- 解析

def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _parse_size(txt: str) -> str:
    """从文本里抠出文件大小，如 '5 MB' / '154.69 KB'。"""
    m = re.search(r"(\d+(?:\.\d+)?)\s*(TB|GB|MB|KB|B)\b", txt or "", re.I)
    return f"{m.group(1)} {m.group(2).upper()}" if m else ""


def parse_files(html: str, class_id: str, folder_id: str = "") -> dict:
    """解析 Files 页面 → {folder, breadcrumb, items, folders, files}。"""
    soup = BeautifulSoup(html, "html.parser")

    # 去掉干扰（Cookie 弹窗等会让文字混杂）
    for sel in (".cookie-consent-wrapper", ".f-cookie-consent-banner",
                "#cookie-preferences-modal", "script", "style",
                ".f-layout-nav", "footer"):
        for el in soup.select(sel):
            el.decompose()

    items: list[dict] = []
    seen: set[str] = set()

    for row in soup.select("div.row.file"):
        cls = " ".join(row.get("class") or [])
        a = row.find("a", href=True)
        if a is None:
            continue

        href = a["href"]
        # 名字：文件夹在 .hstack，文件在 .details（去掉 " by 作者"）
        name_el = row.select_one(".hstack") or row.select_one(".title-link") \
            or row.select_one(".details")
        name = _clean(name_el.get_text(" ")) if name_el else ""
        if not name:
            name = _clean(a.get_text(" "))
        if not name:
            continue

        # ── 文件夹 ──
        mf = re.search(r"/files/folder/(\d+)", href)
        if mf:
            fid = mf.group(1)
            key = f"d:{fid}"
            if key in seen:
                continue
            seen.add(key)
            when = ""
            mw = re.search(r"([A-Z][a-z]{2} \d{1,2}, \d{4} at \d{1,2}:\d{2} [AP]M)",
                           _clean(row.get_text(" ")))
            if mw:
                when = mw.group(1)
            items.append({
                "kind": "folder", "name": name, "folderId": fid,
                "url": f"{config.BASE_URL}/student/classes/{class_id}"
                       f"/files/folder/{fid}",
                "modified": when,
            })
            continue

        # ── 文件 ──
        # 名字后面可能带 " by 作者"
        author = ""
        details_el = row.select_one(".details")
        if details_el:
            dtxt = _clean(details_el.get_text(" "))
            m2 = re.match(r"^(.*?)\s+by\s+(.+)$", dtxt)
            if m2:
                name, author = _clean(m2.group(1)), _clean(m2.group(2))

        size = ""
        sz_el = row.select_one(".size")
        if sz_el:
            size = _clean(sz_el.get_text(" "))
        if not size:
            size = _parse_size(_clean(row.get_text(" ")))

        when = ""
        mw = re.search(r"([A-Z][a-z]{2} \d{1,2}, \d{4} at \d{1,2}:\d{2} [AP]M)",
                       _clean(row.get_text(" ")))
        if mw:
            when = mw.group(1)

        # 直链：优先 S3；否则用页面链接（可能需要一次跳转）
        url = href
        if url.startswith("/"):
            url = config.BASE_URL + url

        if url in seen:
            continue
        seen.add(url)
        items.append({
            "kind": "file", "name": name, "url": url,
            "size": size, "author": author, "modified": when,
        })

    # ── 面包屑（便于显示当前位置）──
    crumbs: list[dict] = [{"name": "根目录", "folderId": ""}]
    if folder_id:
        for cr in soup.select(".breadcrumb a, .f-breadcrumb a, [class*=breadcrumb] a"):
            t = _clean(cr.get_text(" "))
            h = cr.get("href") or ""
            m3 = re.search(r"/files/folder/(\d+)", h)
            if t and t not in ("Files", "Home"):
                crumbs.append({"name": t, "folderId": m3.group(1) if m3 else ""})

    folders = [x for x in items if x["kind"] == "folder"]
    files = [x for x in items if x["kind"] == "file"]

    return {
        "ok": True,
        "classId": class_id,
        "folderId": folder_id,
        "breadcrumb": crumbs,
        "folders": folders,
        "files": files,
        "items": items,
        "total": len(items),
    }


# ---------------------------------------------------------------- 抓取

def _cache_key(cid: str, fid: str) -> str:
    return f"{cid}/{fid}"


def list_files(class_id: str, folder_id: str = "",
               use_cache: bool = True) -> dict:
    """列出某课程 Files 的目录内容（含子文件夹）。"""
    key = _cache_key(class_id, folder_id)
    if use_cache:
        with _lock:
            hit = _cache.get(key)
        if hit and time.time() - hit[0] < _CACHE_TTL:
            return hit[1]

    if folder_id:
        url = f"{config.BASE_URL}/student/classes/{class_id}/files/folder/{folder_id}"
    else:
        url = f"{config.BASE_URL}/student/classes/{class_id}/files"

    cd = httpclient.cooldown_remaining()
    if cd > 0:
        return {"ok": False, "error": f"站点冷却中（还剩 {int(cd)} 秒）",
                "folders": [], "files": [], "items": [], "total": 0}

    try:
        s = httpclient.HttpSession(auto_login_on_demand=True)
        try:
            s.open()
            if not s.logged_in:
                return {"ok": False, "error": s.last_error or "未登录",
                        "folders": [], "files": [], "items": [], "total": 0}
            page = s.page()
            page.goto(url)
            html = page.content()
        finally:
            try:
                s.close()
            except Exception:
                pass

        if not html:
            return {"ok": False, "error": "拿不到页面",
                    "folders": [], "files": [], "items": [], "total": 0}

        data = parse_files(html, class_id, folder_id)
        with _lock:
            _cache[key] = (time.time(), data)
        log.info("Files 列表：%s → %d 个文件夹 / %d 个文件",
                 folder_id or "根", len(data["folders"]), len(data["files"]))
        return data

    except Exception as e:
        log.warning("列 Files 失败：%s", e)
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "folders": [], "files": [], "items": [], "total": 0}


def clear_cache() -> None:
    with _lock:
        _cache.clear()


# ---------------------------------------------------------------- 统计

def count_all(class_id: str, max_depth: int = 3) -> dict:
    """递归统计文件夹与文件总数（用于显示「N 个文件」）。"""
    seen_folders = 0
    seen_files = 0
    size_hint: list[str] = []

    def walk(fid: str, depth: int) -> None:
        nonlocal seen_folders, seen_files
        if depth > max_depth:
            return
        d = list_files(class_id, fid)
        if not d.get("ok"):
            return
        seen_files += len(d.get("files", []))
        for f in d.get("files", []):
            if f.get("size"):
                size_hint.append(f["size"])
        for sub in d.get("folders", []):
            seen_folders += 1
            walk(sub.get("folderId", ""), depth + 1)

    walk("", 1)
    return {"folders": seen_folders, "files": seen_files,
            "sizes": size_hint[:20]}


# ---------------------------------------------------------------- 下载

def safe_name(s: str) -> str:
    """把名字变成合法文件名。"""
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", s or "")
    s = re.sub(r"\s+", " ", s).strip(" .")
    return (s or "未命名")[:120]


def save_root(class_name: str = "") -> Path:
    """保存根目录：桌面/ManageBac 资料/<课程名>/"""
    try:
        desktop = Path.home() / "Desktop"
        if not desktop.exists():
            desktop = Path.home() / "桌面"
    except Exception:
        desktop = Path.home()
    root = desktop / "ManageBac 资料"
    if class_name:
        root = root / safe_name(class_name)
    return root


def download_item(item: dict, cookies: dict[str, str] | None = None,
                  dest_dir: Path | None = None,
                  referer: str = "") -> dict:
    """下载单个文件项。返回 {ok, name, path, size, error}。"""
    import urllib.error
    import urllib.request

    url = item.get("url") or ""
    name = safe_name(item.get("name") or "file")
    out = {"ok": False, "name": name, "path": "", "size": 0, "error": ""}
    if not url:
        out["error"] = "缺少链接"
        return out

    dest_dir = dest_dir or save_root()
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        out["error"] = f"建目录失败：{e}"
        return out

    dest = dest_dir / name
    # 重名自动加序号
    if dest.exists():
        stem, suf = dest.stem, dest.suffix
        for i in range(1, 100):
            cand = dest_dir / f"{stem} ({i}){suf}"
            if not cand.exists():
                dest = cand
                break

    ck = cookies or httpclient.browser_cookies_dict()
    header = "; ".join(f"{k}={v}" for k, v in ck.items())
    req = urllib.request.Request(url, headers={
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/153.0.0.0 Safari/537.36 Edg/153.0.0.0"),
        "Cookie": header,
        "Referer": referer or f"{config.BASE_URL}/student/home",
        "Accept": "*/*",
    })
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            data = r.read()
        if not data:
            out["error"] = "下载到 0 字节"
            return out
        dest.write_bytes(data)
        out.update(ok=True, path=str(dest), size=len(data))
        return out
    except urllib.error.HTTPError as e:
        out["error"] = f"HTTP {e.code}"
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out
