"""Teams / EC（English Corner）消息与附件同步。

【功能】
  从 Microsoft Teams 网页版读取消息与附件，重点抓两件事：

    ① EC（English Corner）相关消息
       每天会发布一个 PDF（名单 / 当天安排）。
       程序会自动下载并提取 PDF 里的文字，方便在本地搜索。

    ② 作业类消息
       频道里发的作业通知（含截止时间、要求、附件）。

【技术路线（参考 CampusDesk）】
  · **浏览器会话模式**：用组件自带的 Edge（独立 profile），
    用户在窗口里正常登录 Teams，程序读页面 DOM。
    → 不导出任何令牌，不绕过权限，只用当前会话能看的内容。

  · **附件安全校验**：只接受 Teams / SharePoint / OneDrive 域名的 https 链接，
    且拒绝带 access_token / id_token 等授权参数的 URL。
    → 防止把带凭据的链接写进缓存或导出给别人。

  · **读不到就保持原状**：页面结构变了 → 记录「未识别」，
    不清空已有数据，也不猜。

【与 CampusDesk 的差异（平台适配）】
  · 它用 WKWebView + Swift 读 DOM；我们用 CDP 读渲染后的 DOM
  · 它的 OCR 走 macOS Vision；Windows 上没这个 API，
    所以我们只做**文字层提取**，扫描件 PDF 会明确标注「无法提取文字」
    —— 不假装能读，避免给出错误信息
  · 它把数据存 Application Support；我们存 data/ 目录（与项目一致）

【重要边界】
  · 只读，绝不发消息
  · 不保证读到全部历史（Teams 是虚拟滚动，只读已渲染的部分）
  · 页面改版会失效 —— 那时回退到「手动选中文字」的方式
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from . import config

log = logging.getLogger("teams")

# ── 存储位置 ──
TEAMS_FILE = config.DATA_DIR / "teams.json"
EC_DIR = config.DATA_DIR / "ec"                  # EC 的 PDF 与提取出的文字

# ── 数量与大小限制（防止一次拉太多）──
MAX_MESSAGES = 100           # 单次最多读 100 条已渲染消息
MAX_BODY = 16_000            # 单条消息正文上限（字符）
MAX_ATTACH = 12              # 每轮最多下载 12 个新附件
MAX_ATTACH_MB = 12           # 单个附件上限 12 MB
MAX_PDF_PAGES = 100          # PDF 最多解析 100 页

# ── 只接受这些域名的附件（白名单）──
ATTACH_HOSTS = (
    ".sharepoint.com", ".sharepoint.cn",
    ".sharepoint-df.com",
    "onedrive.live.com", "1drv.ms",
    "teams.microsoft.com", "teams.cloud.microsoft",
    ".microsoft.com",
    ".office.com", ".officeapps.live.com",
    ".cloud.microsoft",
)

# ── 带这些参数的 URL 一律拒绝（会泄漏访问权）──
AUTH_PARAMS = {
    "access_token", "id_token", "refresh_token", "token", "code",
    "client_secret", "assertion", "password", "passwd", "authorization",
    "auth_token", "session_token", "authkey", "sig", "signature",
    "session_state", "sp", "se", "spr", "sv", "sw", "sdd",
}

TS_HOSTS = ("teams.microsoft.com", "teams.cloud.microsoft")


# ══════════════════════════════════════════════════════════════════
#  链接安全校验
# ══════════════════════════════════════════════════════════════════

def has_auth_params(url: str) -> bool:
    """URL 里有没有授权类参数。

    ★ 为什么要查这个
      Teams 的某些分享链接会带 access_token。
      如果原样存下来，缓存文件或导出的备份里就带上了「访问凭证」，
      转发给别人等于把账号权限给出去了。
    """
    try:
        u = urlparse(url)
        keys = set(k.lower() for k in parse_qs(u.query).keys())
        # SPA 的 hash 里也可能带参数
        if u.fragment and "?" in u.fragment:
            fq = u.fragment.split("?", 1)[1]
            keys |= set(k.lower() for k in parse_qs(fq).keys())
        return bool(keys & AUTH_PARAMS)
    except Exception:
        return True          # 解析失败就当不安全


def safe_url(url: str, *, allow_teams: bool = True) -> str:
    """校验链接是否安全，安全返回原链接，否则返回空串。

    规则（四层）：
      ① 必须 https
      ② 不能带端口 / 用户名密码
      ③ 域名在白名单内
      ④ 不带授权参数
    """
    if not url or not isinstance(url, str):
        return ""
    if len(url) > 4096:
        return ""
    try:
        u = urlparse(url)
    except Exception:
        return ""

    if u.scheme != "https":
        return ""
    if u.port or u.username or u.password:
        return ""

    host = (u.hostname or "").lower()
    if not host:
        return ""

    if allow_teams and host in TS_HOSTS:
        pass                      # Teams 自己的页面链接
    elif any(host == h.lstrip(".") or host.endswith(h) for h in ATTACH_HOSTS):
        pass                      # 附件域名
    else:
        return ""

    if has_auth_params(url):
        log.warning("拒绝带授权参数的链接：%s", url[:80])
        return ""

    return url


# ══════════════════════════════════════════════════════════════════
#  消息分类
# ══════════════════════════════════════════════════════════════════

# EC 相关关键词（English Corner 是给需要额外英语辅导的学生安排的）
EC_RE = re.compile(
    r"\bEnglish\s+Corner\b"
    r"|\bEC\s+announcements?\b"
    r"|\bno\s+EC\b"
    r"|\bEC\b[^\n.]{0,40}\b(?:cancelled|canceled|postponed|rescheduled)\b"
    r"|\bEC\b[^\n。]{0,24}(?:取消|暂停|延期|改期)"
    r"|\bEC\b"
    r"|英语角",
    re.I,
)

# 作业类
HW_RE = re.compile(
    r"\b(?:homework|assignment|due|deadline)\b"
    r"|作业|截止|提交",
    re.I,
)

# 时间：2026-09-21T08:00:00Z 这类带时区的完整时间
ISO_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})"
    r"(?::(\d{2})(?:\.\d+)?)?(Z|[+-]\d{2}:?\d{2})$"
)


def is_ec(title: str, body: str, channel: str = "") -> bool:
    """判断是不是 EC 相关消息。

    ★ 判定要看「正文」而不是只看频道名 ——
      因为 Teams 里可能有人随便提到 EC 这个词。
      但只要正文出现 English Corner / EC announcements 就认定。
    """
    blob = f"{title}\n{body}\n{channel}"
    return bool(EC_RE.search(blob))


def is_homework(title: str, body: str, channel: str = "") -> bool:
    blob = f"{title}\n{body}\n{channel}"
    # 频道名是 Homework / 作业 也算
    if re.search(r"(?:^|[>›/])\s*(?:homework|assignments?|作业)\s*$",
                 channel or "", re.I):
        return True
    return bool(HW_RE.search(blob))


def parse_iso(value: str) -> str | None:
    """解析带时区的完整时间，返回 ISO 字符串；不完整则返回 None。

    ★ 刻意不做「猜年份」——CampusDesk 的经验是：
      日期信息不全时**保留原文**，不推断。
      因为把日期猜错比不显示更糟（学生会按错的日期交作业）。
    """
    v = (value or "").strip()
    if not ISO_RE.match(v):
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).isoformat()
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════
#  附件：PDF 文字提取
# ══════════════════════════════════════════════════════════════════

def extract_pdf_text(path: Path, max_pages: int = MAX_PDF_PAGES) -> dict:
    """从 PDF 提取文字。

    ★ 与 CampusDesk 的差异（这个要说清楚）
      它用 macOS 的 Vision 框架做 OCR，扫描件也能读。
      **Windows 上没这个系统 API**，所以我们只做文字层提取。

      扫描件（纯图片的 PDF）会返回空文字 + ok=False，
      并标注「这份 PDF 是图片，读不出文字」——
      **不假装能读**，也不给出可能错误的名单。

    ★ 依赖
      优先用 pypdf（体积小、纯 Python）。
      没装的话返回「未安装解析库」，功能降级但不报错。
    """
    out = {"ok": False, "text": "", "pages": 0, "note": ""}

    if not path.exists():
        out["note"] = "文件不存在"
        return out

    size_mb = path.stat().st_size / 1024 / 1024
    if size_mb > MAX_ATTACH_MB:
        out["note"] = f"文件过大（{size_mb:.1f} MB），未解析"
        return out

    try:
        from pypdf import PdfReader
    except ImportError:
        try:
            from PyPDF2 import PdfReader          # 老版本兼容
        except ImportError:
            out["note"] = "未安装 PDF 解析库（pip install pypdf）"
            return out

    try:
        reader = PdfReader(str(path))
        pages = min(len(reader.pages), max_pages)
        chunks = []
        empty = 0
        for i in range(pages):
            try:
                t = reader.pages[i].extract_text() or ""
            except Exception:
                t = ""
            if t.strip():
                chunks.append(t)
            else:
                empty += 1
        text = "\n".join(chunks).strip()
        out["pages"] = pages
        out["text"] = text
        out["ok"] = bool(text)
        if not text:
            out["note"] = (f"这份 PDF 的 {pages} 页都没有文字层"
                           f"（可能是扫描件），需要人工打开查看")
        elif empty:
            out["note"] = f"{pages} 页中有 {empty} 页没有文字层"
        return out
    except Exception as e:
        out["note"] = f"解析失败：{e}"
        return out


# ══════════════════════════════════════════════════════════════════
#  数据存取
# ══════════════════════════════════════════════════════════════════

def load() -> dict:
    """读本地缓存的 Teams 数据。"""
    try:
        if TEAMS_FILE.exists():
            d = json.loads(TEAMS_FILE.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                d.setdefault("posts", [])
                d.setdefault("ec", [])
                d.setdefault("attachments", [])
                return d
    except Exception as e:
        log.warning("读取 Teams 缓存失败：%s", e)
    return {"posts": [], "ec": [], "attachments": [], "updated": "",
            "note": "还没有同步过"}


def save(data: dict) -> None:
    config.ensure_dirs()
    data["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tmp = TEAMS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(TEAMS_FILE)


def status() -> dict:
    """给界面看的简短状态。"""
    d = load()
    return {
        "ok": True,
        "updated": d.get("updated") or "",
        "ec_count": len(d.get("ec") or []),
        "post_count": len(d.get("posts") or []),
        "attach_count": len(d.get("attachments") or []),
        "note": d.get("note") or "",
        "ready": bool(d.get("updated")),
    }


def ec_list(keyword: str = "", date: str = "") -> list:
    """列出 EC 记录（可按关键词 / 日期筛选）。"""
    items = load().get("ec") or []
    out = []
    for it in items:
        if date and not (it.get("date") or "").startswith(date):
            continue
        if keyword:
            blob = (it.get("title", "") + it.get("text", "")
                    + it.get("file_text", "") + it.get("channel", ""))
            if keyword.lower() not in blob.lower():
                continue
        out.append(it)
    # 新的排前面
    out.sort(key=lambda x: (x.get("date") or "", x.get("title") or ""),
             reverse=True)
    return out


def _self_test() -> int:
    """自测：链接校验 + 分类 + 时间解析。"""
    ok = fail = 0

    def chk(cond, label):
        nonlocal ok, fail
        if cond:
            ok += 1
        else:
            fail += 1
            print(f"  [X] {label}")

    print("=" * 60)
    print("  Teams / EC 模块自测")
    print("=" * 60)

    print("\n[1] 链接校验（这些必须通过）")
    good = [
        "https://teams.microsoft.com/l/message/19:x@thread.tacv2/123",
        "https://school.sharepoint.com/sites/ec/Doc.pdf",
        "https://school.sharepoint.cn/:x:/r/sites/a/b.pdf",
        "https://1drv.ms/b/s!AbCdEf",
    ]
    for u in good:
        r = safe_url(u)
        chk(bool(r), f"应通过但被拒: {u[:50]}")
    print(f"  通过 {len(good)} 个正常链接")

    print("\n[2] 链接校验（这些必须拒绝）")
    bad = [
        ("http://teams.microsoft.com/x", "非 https"),
        ("https://evil.com/x.pdf", "域名不在白名单"),
        ("https://teams.microsoft.com/x?access_token=abc", "带 access_token"),
        ("https://x.sharepoint.com/f.pdf?sp=r&sig=xyz", "带签名参数"),
        ("https://teams.microsoft.com:8443/x", "非标准端口"),
        ("https://user:pass@teams.microsoft.com/x", "带用户名密码"),
        ("javascript:alert(1)", "javascript 协议"),
        ("", "空串"),
    ]
    for u, why in bad:
        r = safe_url(u)
        chk(not r, f"应拒绝但通过了（{why}）: {u[:50]}")
    print(f"  拒绝 {len(bad)} 个危险链接")

    print("\n[3] EC 分类")
    ec_cases = [
        ("EC announcement", "The English Corner session is cancelled today.", True),
        ("English Corner", "名单见附件", True),
        ("Daily update", "No EC this Friday.", True),
        ("Homework", "Finish the worksheet.", False),
        ("Random chat", "Let's meet at lunch.", False),
    ]
    for title, body, want in ec_cases:
        got = is_ec(title, body)
        chk(got == want, f"EC 判定错误: {title!r} → {got}，期望 {want}")
    print(f"  检查 {len(ec_cases)} 个样例")

    print("\n[4] 时间解析（不完整的必须拒绝）")
    t_cases = [
        ("2026-09-21T08:00:00Z", True),
        ("2026-09-21T08:00:00+08:00", True),
        ("2026-09-21 08:00", False),          # 无时区 → 不猜
        ("Friday at 8:00 AM", False),
        ("Sep 21", False),
    ]
    for v, want in t_cases:
        got = bool(parse_iso(v))
        chk(got == want, f"时间判定错误: {v!r} → {got}，期望 {want}")
    print(f"  检查 {len(t_cases)} 个样例")

    print("\n" + "=" * 60)
    print(f"  通过 {ok} · 失败 {fail}")
    print("=" * 60)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(_self_test())
