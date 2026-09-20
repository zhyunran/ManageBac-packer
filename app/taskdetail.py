"""任务详情 —— 抓取单个作业的完整信息（附件 / 老师要求 / 成绩）。

【用户要求】
    点击作业展开一个菜单，包括：
        * 附件（如果有）
        * 老师要求
        * GPA（若已被登记）
    然后放一个按键，点击跳转原链接。

【实现说明】
    ManageBac 的任务详情页（`/core_tasks/{id}`）是完整 HTML，
    用纯 HTTP 就能拿到 —— 不需要浏览器。

    抓取结果按任务缓存到磁盘（`data/task_detail/<tid>.json`），
    避免每次点开都重新请求（既慢又可能触发限速）。
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path

from bs4 import BeautifulSoup

from . import config, httpclient

log = logging.getLogger("taskdetail")

DETAIL_DIR = config.DATA_DIR / "task_detail"
# 缓存有效期（秒）—— 一天内不重复抓同一任务
CACHE_TTL = 24 * 3600

_lock = threading.RLock()


# ---------------------------------------------------------------- 解析

def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _tid_of(url: str) -> str:
    m = re.search(r"/core_tasks/(\d+)", url or "")
    return m.group(1) if m else ""


def _has_cookie(s) -> bool:
    """会话里有没有登录 Cookie（不发网络请求，纯本地判断）。"""
    try:
        return any(c.name == "_managebac_session" for c in s._cj)
    except Exception:
        # 拿不到就保守认为有，后续请求会自己暴露问题
        return True


def _looks_like_login(html: str) -> bool:
    """判断返回的 HTML 是不是登录页（说明会话失效）。

     比「额外 GET 一个首页去探测」快得多 —— 我们本来就要抓目标页面，
      从它的内容就能判断登录态，不需要多花一个请求。
    """
    if not html:
        return True
    head = html[:8000]
    # 登录页特征
    if "session_form" in head:
        return True
    if re.search(r'<form[^>]+action=["\'][^"\']*/sessions?["\']', head):
        return True
    if re.search(r'<input[^>]+name=["\']password["\']', head):
        return True
    # 反向确认：正常页面应该有 ManageBac 的特征元素
    return "fusion" not in html.lower() and "managebac" not in html.lower()


def parse_detail(html: str, url: str = "") -> dict:
    """从任务详情页 HTML 里提取需要的信息。

    【实测结构】
        标题   : h1（课程名）、卡片里 .h4.title 才是任务名
        描述   : .core-task-details 后面 .h4（"Description"）
                 → .redactor-styles / .fr-view 里的内容
        附件   : 描述里的 <a class="fr-file" data-name="xxx.pdf">
        成绩   : .assessment-cell（兄弟节点，与列表页一致）
        截止   : .due-date .due / .due.regular
    """
    soup = BeautifulSoup(html, "html.parser")

    out: dict = {
        "url": url,
        "title": "",
        "description": "",
        "attachments": [],
        "grade": {},
        "due_text": "",
        "category": "",
        "kind": "",
        "status": "",
    }

    # 移除干扰元素（Cookie 弹窗、菜单、页脚）—— 否则描述会抓到它们
    for sel in ("script", "style", ".cookie-consent-wrapper",
                ".f-cookie-consent-banner", "footer", nav_sel(),
                ".modal", "#cookie-preferences-modal"):
        for el in soup.select(sel):
            el.decompose()

    # ---- 任务名：卡片里的 .h4.title ----
    card = soup.select_one(".fusion-card-item.short-assignment") \
        or soup.select_one(".core-task-show .fusion-card-item")
    if card:
        t = card.select_one(".h4.title")
        if t:
            out["title"] = _clean(t.get_text(" "))
        # 分类 / 类型 / 状态
        labels = [_clean(l.get_text(" "))
                  for l in card.select(".labels-set .label")]
        labels = [l for l in labels if l]
        if labels:
            out["category"] = labels[0]
            if len(labels) > 1:
                out["kind"] = labels[1]
            for l in labels:
                if l.lower() in ("pending", "submitted", "late", "not submitted"):
                    out["status"] = l
        due = card.select_one(".due")
        if due:
            out["due_text"] = _clean(due.get_text(" "))

    # ---- 描述 + 附件 ----
    desc_box = None
    for h in soup.find_all(["div", "h2", "h3", "h4", "header"]):
        txt = _clean(h.get_text(" "))
        if txt in ("Description", "Details", "Task Details", "Instructions"):
            # 描述内容在紧邻的兄弟容器里
            for sib in h.find_next_siblings():
                cls = " ".join(sib.get("class") or [])
                if "redactor" in cls or "fr-view" in cls or "show-more" in cls:
                    desc_box = sib
                    break
                if sib.name == "div":
                    inner = sib.select_one(".redactor-styles, .fr-view")
                    if inner:
                        desc_box = inner
                        break
            if desc_box:
                break

    if desc_box is None:
        desc_box = soup.select_one(".redactor-styles.fr-view") \
            or soup.select_one(".redactor-styles")

    if desc_box is not None:
        # 先收集附件（在删除链接之前）
        seen: set[str] = set()
        for a in desc_box.find_all("a", href=True):
            href = a["href"] or ""
            cls = " ".join(a.get("class") or [])
            is_attach = ("fr-file" in cls
                         or "fr-attachment" in cls
                         or "/attachments/" in href)
            if not is_attach:
                continue
            full = href if href.startswith("http") else config.BASE_URL + href
            if full in seen:
                continue
            seen.add(full)

            # 文件名：优先 data-name（最准），其次文件名 span，最后从 URL 猜
            name = _clean(a.get("data-name") or "")
            if not name:
                inner = a.select_one(".fr-inner")
                if inner:
                    name = _clean(inner.get_text(" "))
            if not name:
                name = _name_from_href(href)
            if not name:
                name = "附件"

            size = ""
            sz = a.select_one(".fr-file-size")
            if sz:
                size = _clean(sz.get_text(" "))
            if not size:
                m = re.search(r"([\d.]+\s*(?:KB|MB|GB|B))",
                              _clean(a.get_text(" ")), re.I)
                if m:
                    size = m.group(1)

            out["attachments"].append({"name": name, "url": full, "size": size}
            )

        # 描述正文：把附件链接换成「 文件名」，避免 URL 污染文本
        for a in desc_box.find_all("a", href=True):
            cls = " ".join(a.get("class") or [])
            if "fr-file" in cls or "fr-attachment" in cls:
                nm = _clean(a.get("data-name") or "") or "附件"
                a.replace_with(soup.new_string(f"  {nm} "))
            else:
                a.replace_with(soup.new_string(" " + _clean(a.get_text(" ")) + " "))

        body = _clean(desc_box.get_text(" "))
        # 去掉 "Show More / Show Less" 之类的按钮文字
        body = re.sub(r"\b(Show More|Show Less)\b", " ", body)
        out["description"] = _clean(body)[:1500]

    # ---- 成绩 ----
    cell = soup.select_one(".assessment-cell")
    if cell:
        g = _clean(cell.get_text(" "))
        if g:
            out["grade"]["text"] = g
            m = re.search(r"([A-F])\s+([\d.]+)\s*/\s*([\d.]+)", g)
            if m:
                out["grade"]["level"] = m.group(1)
                out["grade"]["score"] = float(m.group(2))
                out["grade"]["max"] = float(m.group(3))
                try:
                    out["grade"]["percent"] = round(float(m.group(2)) / float(m.group(3)) * 100, 2)
                except ZeroDivisionError:
                    pass
    if not out["grade"] and card:
        gc = card.select_one(".assessment-cell") or card.find_next_sibling(class_=lambda c: c and "assessment-cell" in " ".join(c))
        if gc:
            g = _clean(gc.get_text(" "))
            if g:
                out["grade"]["text"] = g

    return out


def nav_sel() -> str:
    """导航/菜单选择器（这些区域不该进入描述）。"""
    return (".f-menu, .f-layout-nav, nav, header, .f-page-header, "
            ".skip-to-content, .f-hero, .sidebar")


def _name_from_href(href: str) -> str:
    """从 /attachments/<base64> 里解出文件名。"""
    try:
        import base64

        m = re.search(r"/attachments/([A-Za-z0-9_\-=%]+)", href or "")
        if not m:
            return ""
        raw = m.group(1)
        # 补 padding
        pad = "=" * (-len(raw) % 4)
        try:
            dec = base64.b64decode(raw + pad, validate=False)
        except Exception:
            return ""
        # base64 里通常含 "path:filename" 形式
        txt = dec.decode("utf-8", "replace")
        parts = re.findall(r"[\w\u4e00-\u9fff\-. ]+\.[A-Za-z0-9]{2,5}", txt)
        return parts[-1].strip() if parts else ""
    except Exception:
        return ""


# ---------------------------------------------------------------- 缓存

def _cache_file(tid: str) -> Path:
    return DETAIL_DIR / f"{tid}.json"


def _read_cache(tid: str) -> dict | None:
    if not tid:
        return None
    try:
        f = _cache_file(tid)
        if not f.exists():
            return None
        if time.time() - f.stat().st_mtime > CACHE_TTL:
            return None
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_cache(tid: str, data: dict) -> None:
    if not tid:
        return
    try:
        DETAIL_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _cache_file(tid).with_suffix(f".json.tmp.{threading.get_ident()}")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        for i in range(5):
            try:
                tmp.replace(_cache_file(tid))
                break
            except PermissionError:
                if i == 4:
                    raise
                time.sleep(0.2 * (i + 1))
    except Exception as e:
        log.warning("写任务详情缓存失败：%s", e)


# ---------------------------------------------------------------- 抓取

def fetch(url: str, force: bool = False) -> dict:
    """抓取（或读缓存）某个任务的详情。

    返回 dict，含 title / description / attachments / grade，
    以及 ok / error 状态。**不抛异常**，方便直接返回给前端。
    """
    tid = _tid_of(url)
    if not tid and not url:
        return {"ok": False, "error": "缺少任务链接"}

    with _lock:
        if not force:
            c = _read_cache(tid)
            if c:
                c["ok"] = True
                c["cached"] = True
                return c

    # 冷却期内不请求，返回空壳（前端会提示）
    cd = httpclient.cooldown_remaining()
    if cd > 0:
        return {"ok": False, "error": f"站点冷却中（还剩 {int(cd)} 秒）",
                "attachments": [], "description": "", "grade": {}}

    try:
        #  性能关键：不要把 auto_login_on_demand 设成 True！
        #
        #   open() 里会调用 check_login()，而那个会**额外 GET 一个
        #   476 KB 的 /student/home 页面** 来判断登录态 —— 实测占
        #   整个流程的 70%（1294 ms / 1860 ms）。
        #
        #   但我们要抓的就是一个需要登录的页面 —— 直接请求它，
        #   如果被踢到 /login 就说明会话失效了。**不需要额外探测。**
        #
        #   所以：
        #     auto_login_on_demand=False  → open() 只装载 Cookie，不发请求
        #     然后直接 goto(url)            → 一个请求搞定
        #     如果发现拿到的是登录页        → 才去自动登录一次，然后重试
        s = httpclient.HttpSession(auto_login_on_demand=False)
        try:
            #  verify=False → 只装载 Cookie，**不发那个 476 KB 的首页探测**
            #   登录态由下面那次真实请求（详情页本身）来判断
            s.open(verify=False)
            if not _has_cookie(s):
                return {"ok": False, "error": "没有保存的登录状态",
                        "attachments": [], "description": "", "grade": {}}

            page = s.page()
            page.goto(url)
            html = page.content()

            #  拿到的是登录页 → 会话真的失效了，这时才去登录（只试一次）
            if _looks_like_login(html):
                log.info("详情页拿到登录页，会话已失效，尝试自动登录一次")
                s.close()
                s = httpclient.HttpSession(auto_login_on_demand=True)
                s.open()
                if not s.logged_in:
                    return {"ok": False,
                            "error": s.last_error or "未登录",
                            "attachments": [], "description": "", "grade": {}}
                page = s.page()
                page.goto(url)
                html = page.content()
        finally:
            try:
                s.close()
            except Exception:
                pass

        if not html:
            return {"ok": False, "error": "拿不到页面内容",
                    "attachments": [], "description": "", "grade": {}}

        if _looks_like_login(html):
            return {"ok": False, "error": "登录状态已失效",
                    "attachments": [], "description": "", "grade": {}}

        data = parse_detail(html, url)
        data["ok"] = True
        _write_cache(tid, data)
        log.info("任务详情：%s（附件 %d 个）", data.get("title", "")[:30],
                 len(data.get("attachments", [])))
        return data

    except Exception as e:
        log.warning("抓任务详情失败 %s：%s", url, e)
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "attachments": [], "description": "", "grade": {}}


def clear_cache() -> int:
    n = 0
    try:
        for f in DETAIL_DIR.glob("*.json"):
            try:
                f.unlink()
                n += 1
            except Exception:
                pass
    except Exception:
        pass
    return n
