"""文件下载模块 —— 把 ManageBac 上的附件/资料下载到本地。

【两类下载源】（均由 probe_files2.py 实测确认）
  1. 任务附件
       /attachments/<base64>      ← 加密重定向链接，浏览器访问即下载
       例：Pre-Physics_Homework2.pdf
  2. Files 网盘（课程资料）
       文件夹：/student/classes/{cid}/files/folder/{fid}
       文件：  https://...s3.cn-north-1.amazonaws.com.cn/...   ← 直链
       例：Introduction_Class_-_IDS10.pptx

下载通过**浏览器会话**进行（复用已登录的 Cookie），
因此不需要额外的鉴权处理。
"""
from __future__ import annotations

import base64
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

from . import cdp, config

log = logging.getLogger(__name__)

# 下载默认保存位置：桌面下的「ManageBac 资料」文件夹
DESKTOP_NAME = "ManageBac 资料"

_SAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def desktop_dir() -> Path:
    """获取桌面路径（优先用 SHGetFolderPath，兼容 OneDrive 重定向）。"""
    try:
        import ctypes

        buf = ctypes.create_unicode_buffer(260)
        ctypes.windll.shell32.SHGetFolderPathW(None, 0, None, 0, buf)
        d = Path(buf.value)
        if d.exists():
            return d
    except Exception:
        pass
    return Path.home() / "Desktop"


def download_root() -> Path:
    return desktop_dir() / DESKTOP_NAME


def safe_name(name: str, fallback: str = "file") -> str:
    """把文件名清理成 Windows 合法名称。"""
    n = _SAFE.sub("_", (name or "").strip()).strip(" .")
    n = re.sub(r"\s+", " ", n)
    if not n:
        n = fallback
    # 防止过长
    if len(n) > 150:
        stem, dot, ext = n.rpartition(".")
        n = (stem[:140] + dot + ext) if dot else n[:150]
    return n


def guess_name(url: str, text: str = "") -> str:
    """从链接与显示文本推断文件名。"""
    # 显示文本里常带 "文件名.pdf  137 KB"
    m = re.search(r"([^\s/\\]+\.(?:pdf|docx?|pptx?|xlsx?|zip|rar|7z|txt|csv|"
                  r"jpe?g|png|gif|mp4|mp3|mov|svg))", text or "", re.I)
    if m:
        return safe_name(m.group(1))

    path = unquote(urlparse(url).path)
    cand = Path(path).name
    if cand and "." in cand and len(cand) < 160:
        return safe_name(cand)

    # /attachments/<base64> → 尝试解出里面的文件路径
    if "/attachments/" in url:
        try:
            blob = url.split("/attachments/", 1)[1].split("?")[0]
            pad = "=" * (-len(blob) % 4)
            raw = base64.urlsafe_b64decode(blob + pad).decode("utf-8", "ignore")
            m2 = re.search(r"([^\"'/\\]+\.\w{2,5})", raw)
            if m2:
                return safe_name(m2.group(1))
            m3 = re.search(r'"filename"\s*:\s*"([^"]+)"', raw)
            if m3:
                return safe_name(m3.group(1))
        except Exception:
            pass

    return safe_name(Path(path).name or "download")


def browser_cookies() -> dict[str, str]:
    """取站点 Cookie。

     优先用**已保存的 HTTP 会话**（`data/session_cookies.json`）——
      这样完全不需要启动浏览器，下载瞬间开始。
      没有保存的会话时才退回浏览器方式。
    """
    # ---- 1. 已保存的 HTTP 会话（默认路径，最快）----
    try:
        from . import httpclient

        ck = httpclient.load_cookies()
        if ck:
            d = {c["name"]: c["value"] for c in ck if c.get("value")}
            if "_managebac_session" in d:
                return d
    except Exception as e:
        log.info("读取已保存会话失败，改用浏览器：%s", e)

    # ---- 2. 退回浏览器 ----
    session = cdp.Session(headless=True)
    try:
        session.open()
        page = session.page()
        page.goto(f"{config.BASE_URL}/student/home")
        page.wait_for_timeout(600)
        raw = page.eval("""
          (() => {
            const m = {};
            document.cookie.split(';').forEach(s => {
              const i = s.indexOf('=');
              if (i > 0) m[s.slice(0,i).trim()] = s.slice(i+1).trim();
            });
            return JSON.stringify(m);
          })()
        """)
        import json as _json
        return _json.loads(raw) if raw else {}
    except Exception:
        return {}
    finally:
        session.close()


def download_direct(url: str, dest: Path, cookies: dict[str, str] | None = None,
                    timeout: int = 180) -> dict:
    """从 Python 直连下载（带 Cookie），自动跟随重定向。

    相比浏览器内 fetch，直连不受 CORS 限制，
    因此能处理 ``/attachments/...`` 这类跨域重定向的下载链接。
    """
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, headers={
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/153.0.0.0 Safari/537.36 Edg/153.0.0.0"),
        "Accept": "*/*",
        "Referer": f"{config.BASE_URL}/student/home",
    })
    if cookies:
        req.add_header("Cookie", "; ".join(f"{k}={v}" for k, v in cookies.items()))

    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return {"ok": True, "path": str(dest), "size": len(data)}
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def download_via_browser(url: str, dest: Path, timeout_ms: int = 120000) -> dict:
    """用页面自身发起请求下载（带登录 Cookie），内容 base64 回收。

    适合：同源资源。跨域重定向请改用 download_direct()。
    """
    import json

    session = cdp.Session(headless=True)
    try:
        session.open()
        page = session.page()
        page.goto(f"{config.BASE_URL}/student/home")
        page.wait_for_timeout(800)

        js = """
        (async () => {
          try {
            const r = await fetch(%s, {credentials: 'include'});
            if (!r.ok) return JSON.stringify({ok:false, status:r.status});
            const buf = await r.arrayBuffer();
            const bytes = new Uint8Array(buf);
            let bin = '';
            const CH = 0x8000;
            for (let i = 0; i < bytes.length; i += CH) {
              bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CH));
            }
            return JSON.stringify({ok:true, status:r.status, b64: btoa(bin),
                                   size: bytes.length});
          } catch (e) {
            return JSON.stringify({ok:false, error: String(e)});
          }
        })()
        """ % json.dumps(url)

        raw = page.eval(js, await_promise=True)
        res = json.loads(raw) if raw else {"ok": False, "error": "无返回"}

        if not res.get("ok"):
            return {"ok": False, "error": res.get("error") or f"HTTP {res.get('status')}"}

        data = base64.b64decode(res["b64"])
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return {"ok": True, "path": str(dest), "size": len(data)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        session.close()


def download_one(url: str, dest: Path,
                 cookies: dict[str, str] | None = None) -> dict:
    """统一入口：先直连，失败再退回浏览器内 fetch。"""
    r = download_direct(url, dest, cookies)
    if r.get("ok"):
        return r
    # 直连失败（如需要特定会话头）→ 退回浏览器方式
    r2 = download_via_browser(url, dest)
    if r2.get("ok"):
        return r2
    return {"ok": False, "error": f"{r.get('error')} / {r2.get('error')}"}


def download_all(items: list[dict], subdir: str = "") -> list[dict]:
    """批量下载（自动读取 Cookie，复用同一会话）。"""
    root = download_root()
    if subdir:
        root = root / safe_name(subdir)
    root.mkdir(parents=True, exist_ok=True)

    cookies = browser_cookies()
    results: list[dict] = []
    used: set[str] = set()

    for it in items:
        url = (it.get("url") or "").strip()
        if not url:
            continue
        name = safe_name(it.get("name") or guess_name(url, it.get("text", "")))
        stem, dot, ext = name.rpartition(".")
        base = stem if dot else name
        ext = ("." + ext) if dot else ""
        cand, i = name, 1
        while cand.lower() in used:
            cand = f"{base} ({i}){ext}"
            i += 1
        used.add(cand.lower())

        dest = root / cand
        r = download_one(url, dest, cookies)
        r["name"] = cand
        r["url"] = url[:120]
        results.append(r)
        log.info("下载 %s -> %s", cand, "OK" if r["ok"] else r.get("error"))

    return results


def list_files(cid: str, folder_id: str | None = None) -> list[dict]:
    """列出课程 Files 里的文件与文件夹。

    folder_id 为空 → 根目录；否则列出该文件夹。
    """
    session = cdp.Session(headless=True)
    try:
        session.open()
        page = session.page()

        if folder_id:
            url = f"{config.BASE_URL}/student/classes/{cid}/files/folder/{folder_id}"
        else:
            url = f"{config.BASE_URL}/student/classes/{cid}/files"
        page.goto(url)
        page.wait_for_timeout(4000)

        js = """
        (() => {
          const out = [];
          const seen = new Set();
          document.querySelectorAll('a').forEach(a => {
            const h = a.getAttribute('href') || '';
            const t = (a.innerText || '').trim();
            if (!h || !t) return;
            const isFolder = /\\/files\\/folder\\/\\d+/.test(h);
            const isFile = /s3[^\\s]*amazonaws\\.com[^\\s]*|\\/files\\/file\\/\\d+|\\.(pdf|docx?|pptx?|xlsx?|zip|jpe?g|png|mp4)$/i.test(h);
            if (!isFolder && !isFile) return;
            const key = h + '|' + t;
            if (seen.has(key)) return;
            seen.add(key);
            let name = t.split('\\n')[0].trim();
            let size = '';
            const m = t.match(/\\d+(?:\\.\\d+)?\\s*(?:B|KB|MB|GB)/i);
            if (m) size = m[0];
            out.push({url: h, name: name, size: size,
                      kind: isFolder ? 'folder' : 'file',
                      folderId: (h.match(/folder\\/(\\d+)/) || [])[1] || ''});
          });
          return JSON.stringify(out);
        })()
        """
        raw = page.eval(js)
        items = json.loads(raw) if raw else []

        for it in items:
            u = it["url"]
            if u.startswith("/"):
                it["url"] = config.BASE_URL + u
        return items
    except Exception as e:
        log.warning("列举文件失败：%s", e)
        return []
    finally:
        session.close()


def list_course_files(cid: str, folder_id: str | None = None) -> list[dict]:
    """list_files 的别名，语义更清晰。"""
    return list_files(cid, folder_id)


# ==================================================================
# 任务附件：按需抓取并下载
# ==================================================================

def task_attachments(cid: str, tid: str) -> list[dict]:
    """读取某个任务的附件列表（不下载）。"""
    session = cdp.Session(headless=True)
    try:
        session.open()
        page = session.page()
        page.goto(f"{config.BASE_URL}/student/classes/{cid}/core_tasks/{tid}")
        page.wait_for_timeout(2600)

        js = """
        (() => {
          const out = [];
          const seen = new Set();
          document.querySelectorAll('a').forEach(a => {
            const h = a.getAttribute('href') || '';
            const t = (a.innerText || '').trim();
            if (!/\\/attachments\\//i.test(h)) return;
            if (seen.has(h)) return;
            seen.add(h);
            let name = t.split('\\n')[0].trim();
            if (!name) name = (h.split('/').pop() || '').slice(0, 60);
            let size = '';
            const m = t.match(/\\d+(?:\\.\\d+)?\\s*(?:B|KB|MB|GB)/i);
            if (m) size = m[0];
            out.push({url: h, name: name, size: size, kind: 'attachment'});
          });
          return JSON.stringify(out);
        })()
        """
        raw = page.eval(js)
        items = json.loads(raw) if raw else []
        for it in items:
            u = it["url"]
            if u.startswith("/"):
                it["url"] = config.BASE_URL + u
        return items
    except Exception as e:
        log.warning("读取任务附件失败：%s", e)
        return []
    finally:
        session.close()


def download_task(cid: str, tid: str, title: str = "") -> dict:
    """下载某个任务的**全部附件**（供 UI 一键调用）。

    保存到：桌面\\ManageBac 资料\\<课程/任务名>\\
    """
    items = task_attachments(cid, tid)
    if not items:
        return {"ok": False, "error": "该任务没有附件", "count": 0,
                "files": []}

    sub = safe_name(title)[:60] if title else f"task_{tid}"
    results = download_all(items, subdir=sub)
    ok_n = sum(1 for r in results if r["ok"])
    folder = str((download_root() / sub).resolve()) if sub else str(download_root())
    return {
        "ok": ok_n > 0,
        "count": ok_n,
        "total": len(results),
        "folder": folder,
        "files": [{"name": r["name"], "ok": r["ok"], "size": r.get("size"),
                   "error": r.get("error")} for r in results],
    }


def download_files(cid: str, cid_name: str = "") -> dict:
    """下载某门课程 Files 里的**全部文件**（不含子文件夹）。"""
    items = [x for x in list_files(cid) if x.get("kind") == "file"]
    if not items:
        return {"ok": False, "error": "Files 里没有可下载的文件", "count": 0}
    sub = safe_name(cid_name)[:60] if cid_name else f"class_{cid}"
    results = download_all(items, subdir=sub)
    ok_n = sum(1 for r in results if r["ok"])
    return {
        "ok": ok_n > 0,
        "count": ok_n,
        "total": len(results),
        "folder": str((download_root() / sub).resolve()),
        "files": [{"name": r["name"], "ok": r["ok"], "size": r.get("size"),
                   "error": r.get("error")} for r in results],
    }


def open_folder(path: str) -> None:
    """在资源管理器中打开文件夹。"""
    try:
        import subprocess

        p = Path(path)
        target = p if p.exists() else p.parent
        subprocess.Popen(["explorer", str(target)])
    except Exception as e:
        log.warning("打开文件夹失败：%s", e)
