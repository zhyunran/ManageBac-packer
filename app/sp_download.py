"""从 SharePoint 取 PDF 的字节 —— 唯一的可行路径。

【为什么不能直接 HTTP】

    先用最直白的办法试过：Python `requests` 直接 GET 那个分享链接。
    结果是同一个 26 KB 的 HTML，标题「Redirecting」。

    | 试法                                          | 结果              |
    |-----------------------------------------------|-------------------|
    | 直连分享链接                                   | 26 KB HTML        |
    | 直连 + `?download=1`                           | 同上              |
    | `_layouts/15/download.aspx?id=…`               | 同上              |
    | `_api/web/GetFileByServerRelativeUrl(…)/$value`| 403               |
    | `_api/v2.0/me/drive/root:/…:/content`          | 同上              |
    | CDP 设下载目录 + 点「下载」按钮                 | 按钮点到了，**文件不落盘** |
    | **导航到预览页 → 页面内 fetch**                 | **200 `%PDF-` 4.06 MB** |

    原因：SharePoint 的登录态**不在 ManageBac 的 cookie 里**。
    那个 jar 里只有 `_managebac_session` 一个。
    但 Edge 的 profile 里是登着的 —— 证据是 localStorage 里有

        tmp.auth.v1.<oid>.Token.HTTPS://BEIJING101.SHAREPOINT.COM

    所以只有两种方式能带上登录态：

      1. 让**浏览器自己**导航过去
      2. 在**那个页面的上下文里** eval `fetch(...)`

    两种一起用：导航过去会打开一个预览页（`onedrive.aspx`），
    然后在这个页面上跑 fetch —— 同源，cookie 自动带，
    SharePoint 的脚本还会把自己那份 Bearer token 也补上。

【点「下载」按钮为什么不行】

    它走的是一条 302 → 新导航 的链路，
    `Browser.setDownloadBehavior` 管不到，所以「点了但没落盘」。
    fetch 把字节收回来自己写文件，这条链路没有中间人。
"""
from __future__ import annotations

import base64
import json
import logging
import re
import time
from pathlib import Path
from urllib.parse import quote, urlparse

log = logging.getLogger("sp_download")

#  下载上限（EC 名单一般 1~5 MB；给足余量，防呆）
MAX_MB = 25

#  等预览页出来的上限（秒）
READY_TIMEOUT = 40

#  候选下载地址一次试完
FETCH_JS = r"""
async (urls) => {
  const out = [];
  const b64 = (buf) => {
    const u8 = new Uint8Array(buf);
    let s = '';
    const CH = 0x8000;
    for (let i = 0; i < u8.length; i += CH) {
      s += String.fromCharCode.apply(null, u8.subarray(i, i + CH));
    }
    return btoa(s);
  };
  for (const u of urls) {
    try {
      const r = await fetch(u, {
        credentials: 'include',
        redirect: 'follow',
        headers: {'Accept': '*/*'}
      });
      const buf = await r.arrayBuffer();
      const head = new Uint8Array(buf.slice(0, 5));
      const magic = String.fromCharCode.apply(null, head);
      const cd = r.headers.get('content-disposition') || '';
      out.push({
        url: u, status: r.status,
        ct: (r.headers.get('content-type') || '').slice(0, 60),
        cd: cd.slice(0, 160),
        len: buf.byteLength, magic,
        b64: (magic === '%PDF-') ? b64(buf) : ''
      });
      if (magic === '%PDF-') break;
    } catch (e) {
      out.push({url: u, err: String(e).slice(0, 120)});
    }
  }
  return out;
}
"""

#  在预览页上挖「当前文件是谁」+「它有哪些参数可用」
PROBE_JS = r"""
() => {
  const h = String(location.href);
  const o = {href: h, ids: {}, pdfName: ''};
  try {
    const q = new URL(h).searchParams;
    for (const k of ['id', 'sourcedoc', 'file', 'resid', 'parent']) {
      const v = q.get(k);
      if (v) o.ids[k] = v;
    }
  } catch (e) {}
  const m = h.match(/[?&]share=([^&]+)/);
  if (m) o.ids.share = decodeURIComponent(m[1]);
  //  页面标题就是文件名（实测：预览页 title == "EC Roster 9.18.pdf"）
  o.pdfName = (document.title || '').trim();
  return o;
}
"""

PDF_NAME_RE = re.compile(r"\.pdf\b", re.I)

#  登录页的域名 / 标题特征
LOGIN_HOSTS = ("login.microsoftonline.com", "login.live.com",
               "login.microsoft.com", "adfs.", "sts.")
LOGIN_TITLES = ("登录到您的帐户", "sign in to your account",
                "登录", "sign in", "登录到", "选一个帐户")


def looks_like_login(host: str = "", title: str = "", body: str = "") -> bool:
    """判断是不是被踢到登录页了。

    ★ 为什么要专门判这个：
      站点库（`beijing101.sharepoint.com/sites/…`）和
      个人盘（`beijing101-my.sharepoint.com/:b:/…`）**是两个不同的主机名**，
      SharePoint 的 `FedAuth` cookie 按主机名走 ——
      登了个人盘**不等于**登了站点库。

      实测：个人盘 2 秒出预览页；站点库直接跳到
      `login.microsoftonline.com`，页面标题「登录到您的帐户」。

      碰到这种情况要说清「需要你先去点一次」，
      而不是含糊地报「下载失败」—— 用户根本不知道该怎么办。
    """
    h = (host or "").lower()
    if any(x in h for x in LOGIN_HOSTS):
        return True
    t = (title or "").strip().lower()
    if t and any(x in t for x in LOGIN_TITLES):
        #  「登录」两个字太常见，得配合正文印证
        b = (body or "").lower()
        if any(x in b for x in ("密码", "password", "电子邮件", "email",
                                "帐户", "account", "验证", "verify")):
            return True
    return False


#  在页面上读「我是谁 / 是不是登录页」
PAGE_INFO_JS = r"""
() => ({
  url: String(location.href).slice(0, 220),
  host: location.host,
  title: (document.title || '').trim().slice(0, 120),
  login: !!document.querySelector(
    'input[type=password],#i0116,#loginHotspot,.login-box,[name=loginfmt]'),
  body: (document.body ? document.body.innerText : '')
          .replace(/\s+/g, ' ').slice(0, 200),
})
"""



def is_pdf_name(name: str) -> bool:
    return bool(PDF_NAME_RE.search(name or ""))


def link_from_label(label: str) -> dict | None:
    """从附件卡的 aria-label 里拆出 {name, url}。

    ★ 实测格式 —— 名字与链接用**换行**分隔：

        "EC Roster 9.18.pdf\\nhttps://beijing101-my.sharepoint.com/:b:/g/…?e=xxx"

    但 Teams 改版过，所以除了换行还认 `\\t`、以及「名字后面接 http」的形态。
    链接本身可能**换行断开**（长 URL 会被折行），所以要把它接回去：
    从 `http` 起，一直收到空白为止 —— 中途的换行忽略。
    """
    if not label:
        return None
    s = str(label).replace("\r", "\n")
    m = re.search(r"https?://", s)
    if not m:
        return None

    raw = s[m.start():]
    #  折行的 URL：把空白全去掉（SharePoint 链接里不会有空格）
    url = re.sub(r"\s+", "", raw)

    #  ── 取文件名 ──
    #  ★ 不能按空格切词 —— 名字本身就带空格（"EC Roster 9.18.pdf"），
    #    切了就只剩 "9.18.pdf"。正确做法是**先按行看**，
    #    取第一条像文件名的行（含点号扩展名）。
    #
    #  三种实测形态：
    #    "EC Roster 9.18.pdf\nhttps://…"            单行名字
    #    "EC Roster 9.17.pdf\nEC Roster 9.17.pdf\n…" 名字重复两遍
    #    "9.22 2.pdf\nhttps://…"                     名字里带空格和数字
    head = s[:m.start()].replace("\t", "\n")
    lines = []
    for ln in head.split("\n"):
        t = re.sub(r"\s+", " ", ln).strip()
        if not t:
            continue
        #  形如「名字.扩展名」——扩展名是 2~5 个字母数字
        if re.search(r"\.[A-Za-z0-9]{2,5}$", t):
            lines.append(t)
    if lines:
        name = lines[0]
    else:
        #  没有像文件名的行 → 把整块当一个名字（可能只有标题没有扩展名）
        name = re.sub(r"\s+", " ", head).strip()

    #  再兜一层：还没有就从 URL 尾巴上取
    if not name:
        try:
            tail = urlparse(url).path.rsplit("/", 1)[-1]
            name = quote(tail, safe="")
        except Exception:
            name = ""
    if not name:
        return None
    return {"name": name.strip()[:180], "url": url}


def candidates(link: str, probe: dict | None = None) -> list[str]:
    """给一个分享链接，列出所有值得一试的下载地址。

    ★ 顺序有意义（第一个命中率最高，实测直接就是它）：

      1. 原分享链接 + `download=1`     ← 实测 200 %PDF-
      2. `onedrive.aspx?id=…&download=1`
      3. `download.aspx?id=…`          （老接口）
      4. `_api/web/GetFileByServerRelativeUrl(…)/$value`
      5. `_api/v2.0/me/drive/root:/Documents/<name>:/content`
      6. 当前 href 去掉预览参数 + `download=1`

    留这么多条是为了**接口一旦变**还能自己找回来 ——
    不能只写「已知能用的那一条」。
    """
    out: list[str] = []

    def add(u: str) -> None:
        if u and u not in out:
            out.append(u)

    add(link + ("&" if "?" in link else "?") + "download=1")

    probe = probe or {}
    ids = probe.get("ids") or {}
    try:
        parsed = urlparse(link)
        origin = f"{parsed.scheme}://{parsed.netloc}"
    except Exception:
        origin = ""

    fid = str(ids.get("id") or "")
    if fid and origin:
        q = quote(fid, safe="")
        add(f"{origin}/_layouts/15/onedrive.aspx?id={q}&download=1")
        add(f"{origin}/_layouts/15/download.aspx?id={q}")
        add(f"{origin}/_api/web/GetFileByServerRelativeUrl("
            f"'{quote(fid, safe='/')}')/$value")
        nm = fid.rsplit("/", 1)[-1]
        if nm:
            add(f"{origin}/_api/v2.0/me/drive/root:/"
                f"Documents/{quote(nm)}:/content")

    if ids.get("share") and origin:
        add(f"{origin}/_layouts/15/download.aspx"
            f"?share={quote(str(ids['share']), safe='')}")

    href = str(probe.get("href") or "")
    if "onedrive.aspx" in href:
        add(href.split("&action=")[0] + "&download=1")

    return out


def _page_alive(page) -> bool:
    try:
        return bool(page.eval("1+1"))
    except Exception:
        return False


def fetch_bytes(page, link: str, *, timeout: int = READY_TIMEOUT,
                verbose: bool = False) -> dict:
    """取一个分享链接指向的文件字节。返回 {ok, data?, name?, note?}。

    步骤（每一步都是必需的，少一步就不通）：

      ① 导航到分享链接  →  SharePoint 会开一个**预览页**
      ② 等 `document.title` 变成 `xxx.pdf`
         —— 这是预览页真的渲染好了的信号（比等固定的秒数可靠）
      ③ 在**这个页面的上下文里** fetch 若干候选地址
      ④ 第一个返回 `%PDF-` 的就是我们要的

    ★ 失败了要说清**是哪一种失败**，别一律报「下载失败」：

      · `need_login`  —— 被踢到 login.microsoftonline.com。
        这是**最常见**的一种：个人盘和站点库是两个主机名，
        登了一个不等于登了另一个。
        调用方应该提示用户去点一次、而不是无脑重试。
      · `no_preview`  —— 导航过去了但没渲染出预览页（超时 / 页面结构变了）
      · `no_bytes`    —— 预览页出来了，但所有候选 fetch 都没拿到 %PDF-
    """
    if not _page_alive(page):
        return {"ok": False, "kind": "dead_page", "note": "浏览器页面不可用"}

    try:
        page.goto(link, timeout=60000)
    except Exception as e:
        #  导航抛错不代表失败（SPA 会在加载后自己重定向）
        log.debug("goto 抛错（继续等）：%s", str(e)[:100])

    title = ""
    info: dict = {}
    step = 2.0
    waited = 0.0

    def read_info() -> dict:
        try:
            return page.eval(PAGE_INFO_JS) or {}
        except Exception:
            return {}

    #  页面已经落在 sharepoint 域名上（标题可能是空的）→
    #  这种情况**不必等标题**，直接往下走。
    #
    #  ★ 为什么：站点文档库的链接直接导航过去时，
    #    浏览器会去渲染/下载 PDF，页面本身是**空白的** ——
    #    实测 `document.title === ''`、`body.innerText.length === 0`
    #    而且要一直空着（等 40 秒也是空的）。
    #    原来的判断「等标题变成 xxx.pdf」对站点库**永远等不到**，
    #    于是白等 40 秒后报 no_preview —— 明明文件就在手边。
    #
    #    个人盘分享链接（`:b:`）不一样，它会打开预览页、
    #    标题就是文件名。所以两种都要照顾：
    #      标题出来了 → 立刻走
    #      域名对了但标题一直空 → 也别干等，直接去 fetch
    on_sharepoint = False
    for _ in range(3):
        try:
            on_sharepoint = "sharepoint" in str(
                page.eval("location.host") or "").lower()
        except Exception:
            on_sharepoint = False
        if on_sharepoint:
            break
        time.sleep(1)

    if on_sharepoint:
        #  给预览页 4 秒机会出标题（`:b:` 那种很快），
        #  出不来就直接进入 fetch 阶段
        for _ in range(2):
            time.sleep(step)
            waited += step
            try:
                title = str(page.eval("document.title") or "").strip()
            except Exception:
                title = ""
            if title and is_pdf_name(title):
                if verbose:
                    print(f"    {waited:>4.0f}s  预览页就绪：{title!r}")
                break
        if verbose and not is_pdf_name(title):
            print(f"    {waited:>4.0f}s  站点库空白页（无标题）"
                  f"→ 直接取字节")

    while not on_sharepoint and waited < timeout:
        time.sleep(step)
        waited += step
        try:
            title = str(page.eval("document.title") or "").strip()
        except Exception:
            title = ""

        if title and is_pdf_name(title):
            if verbose:
                print(f"    {waited:>4.0f}s  预览页就绪：{title!r}")
            break

        #  每隔几秒确认一下是不是被踢去登录了 ——
        #  干等 40 秒不如 4 秒就知道原因
        if waited >= 4 and int(waited) % 4 < step:
            info = read_info()
            h = str(info.get("host") or "")
            if looks_like_login(h, str(info.get("title") or ""),
                                str(info.get("body") or "")):
                #  ★ 先看是不是「MFA 注册提示」—— 那个能点掉。
                #    区别：真要密码的登录页上没有「下一步」按钮，
                #    而 MFA 提示页有（第二屏还有一个「跳过设置」）。
                sk = skip_mfa(page, verbose=verbose, timeout=60)
                if sk.get("skipped"):
                    log.info("已跳过二次验证设置（%s）", sk.get("note") or "ok")
                    #  点掉之后重新导航一次，把文件取回来
                    try:
                        page.goto(link, timeout=60000)
                    except Exception:
                        pass
                    time.sleep(2)
                    on_sharepoint = "sharepoint" in str(
                        page.eval("location.host") or "").lower()
                    if on_sharepoint:
                        title = ""
                        break
                if not sk.get("ok"):
                    log.info("二次验证这关过不去：%s", sk.get("note"))
                return {
                    "ok": False, "kind": "need_login", "host": h,
                    "note": sk.get("note")
                            or f"需要先登录 {h} —— 这个域名的登录态还没有",
                }
        if verbose and waited % 8 < step:
            print(f"    {waited:>4.0f}s  title={title[:50]!r}")
    else:
        if not on_sharepoint:
            info = info or read_info()
            if looks_like_login(str(info.get("host") or ""),
                                str(info.get("title") or ""),
                                str(info.get("body") or "")):
                h = str(info.get("host") or "")
                sk = skip_mfa(page, verbose=verbose, timeout=60)
                return {"ok": False, "kind": "need_login", "host": h,
                        "note": sk.get("note")
                                or f"需要先登录 {h}"}
            return {"ok": False, "kind": "no_preview",
                    "note": f"等了 {timeout:.0f} 秒没等到预览页"
                            f"（当前标题 {title[:60]!r}）"}

    try:
        probe = page.eval(PROBE_JS) or {}
    except Exception:
        probe = {}

    urls = candidates(link, probe)
    if not urls:
        return {"ok": False, "kind": "no_urls", "note": "拼不出任何下载地址"}

    try:
        results = page.call(FETCH_JS, urls) or []
    except Exception as e:
        return {"ok": False, "kind": "fetch_error",
                "note": f"页面内取字节失败：{str(e)[:140]}"}

    name = str((probe or {}).get("pdfName") or title or "EC Roster.pdf")

    n_err = 0
    for r in results:
        if r.get("err"):
            n_err += 1
            if verbose:
                print(f"    x {str(r['url'])[:90]}  {r['err']}")
            continue
        if verbose:
            print(f"    {r.get('status')} {r.get('magic')!r} "
                  f"{r.get('len', 0):>10,}B  {str(r['url'])[:78]}")
        if r.get("b64"):
            raw = base64.b64decode(r["b64"])
            if len(raw) > MAX_MB * 1024 * 1024:
                return {"ok": False, "kind": "too_big",
                        "note": f"文件过大（{len(raw)/1e6:.1f} MB）"}
            #  让文件名带扩展名，后面按后缀判断才有依据
            if not is_pdf_name(name):
                name += ".pdf"
            return {"ok": True, "data": raw, "name": name,
                    "url": r.get("url"), "size": len(raw)}

    #  全是网络层错误（跨域被拦的典型表现）→ 说明当前 origin 不对
    if results and n_err == len(results):
        return {"ok": False, "kind": "cross_origin",
                "note": "所有候选都被跨域拦下了 —— 当前页面不在这个域名下"}

    return {"ok": False, "kind": "no_bytes",
            "note": "页面内 fetch 没拿到 %PDF-"
                    f"（试了 {len(urls)} 个地址）"}


def fetch_to_file(page, link: str, dest_dir: Path,
                  name: str = "", *, verbose: bool = False) -> dict:
    """取字节并落盘。返回 {ok, path?, name?, size?, note?}。"""
    r = fetch_bytes(page, link, verbose=verbose)
    if not r.get("ok"):
        return r

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    fname = (name or r.get("name") or "EC Roster.pdf")
    #  去掉路径分隔符，别让名字把文件写到别处去
    fname = re.sub(r'[\\/:*?"<>|]+', "_", fname).strip()[:150]
    if not is_pdf_name(fname):
        fname += ".pdf"

    dest = dest_dir / fname
    try:
        dest.write_bytes(r["data"])
    except Exception as e:
        return {"ok": False, "note": f"写文件失败：{e}"}
    return {"ok": True, "path": str(dest), "name": fname,
            "size": len(r["data"]), "url": r.get("url")}


# ══════════════════════════════════════════════════════════
#  个人盘（OneDrive）整目录拉取
#
#  ★ 为什么需要这条路
#    EC 频道的附件有两种链接：
#
#      A. `beijing101-my.sharepoint.com/:b:/g/personal/…`（个人盘分享）
#         → 能自动化（见 fetch_bytes）
#
#      B. `beijing101.sharepoint.com/sites/Beijing101HighSchool/…`（站点库）
#         → **跳 MFA**，不能自动化
#           （页面原文：「让我们来保护帐户安全，我们将帮助你设置
#             另一种方法来验证你的身份」—— 这是二次验证注册提示）
#
#    所以 B 类链接失败时，回到 A 那条路：把**整个目录列一遍**，
#    按文件名找同名的那份。实测 9.11~9.18 都在个人盘上。
#    （9.21/9.22 只在站点库 —— 那种情况如实报告「需要你手动登一次」。）
# ══════════════════════════════════════════════════════════

#  个人盘常见主机名（按优先级）
PERSONAL_HOSTS = (
    "https://beijing101-my.sharepoint.com",
)

#  列目录 / 列子目录的接口
#
#  ★★ 两个坑，实测撞出来的：
#    ① **不能加 `$select`** —— 加了这个 profile 一律 403。
#       去掉就 200，返回每个文件的完整字段。`$top` 无影响。
#    ② 路径要 **URL 编码**，但 `@a1='…'` 那种别名参数要**带单引号**，
#       否则报「查询字符串 DecodedUrl 缺失或无效」。
LIST_FILES_JS = r"""
async (url) => {
  try {
    const r = await fetch(url, {
      credentials: 'include',
      headers: {'Accept': 'application/json;odata=nometadata'},
    });
    const t = await r.text();
    return {status: r.status, len: t.length, text: t.slice(0, 800000)};
  } catch (e) {
    return {err: String(e).slice(0, 200)};
  }
}
"""


def personal_roots(page, site_path: str = "") -> list[str]:
    """猜几个「可能装着这份文件的个人盘目录」。

    site_path 是站点库那条相对路径，形如
        /sites/Beijing101HighSchool/Shared Documents/ENGLISH CORNER ROSTER/x.pdf
    取「Shared Documents 之后、文件名之前」那一段目录名，
    拼到个人盘的 Documents 下 —— 老师很可能用同一个目录名。

    返回一串 `ServerRelativeUrl`（不含主机名），按可能性排序。
    """
    from urllib.parse import unquote as _uq

    #  个人盘所有者 —— EC 名单是 Emerson 发的，盘是他的
    base = "/personal/emerson_miller_beijing101id_com/Documents"

    roots: list[str] = [base]

    segs = [s for s in (site_path or "").split("/") if s]
    sub: list[str] = []
    for i, s in enumerate(segs):
        if _uq(s).lower().strip() == "shared documents":
            sub = [_uq(x) for x in segs[i + 1:-1]]      # 去掉文件名
            break
    if sub:
        roots.append(base + "/" + "/".join(sub))
        #  只取第一层目录也试一次（有时候中间层名字不一样）
        if len(sub) > 1:
            roots.append(base + "/" + sub[0])

    #  去重保序
    seen, out = set(), []
    for r in roots:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


# ══════════════════════════════════════════════════════════
#  二次验证（MFA）注册提示：跳过它
#
#  ★★★ 这是「首次跑一次、之后不再需要」那件事的落点
#
#  站点文档库的链接（`…/sites/…`）第一次打开会走**两屏**：
#
#      第 1 屏  login.microsoftonline.com
#               「<账号> 让我们来保护帐户安全
#                 我们将帮助你设置另一种方法来验证你的身份」
#               主按钮：`<input type=submit id=idSubmit_ProofUp_Redirect
#                        value="下一步">`
#
#      第 2 屏  mysignins.microsoft.com/register
#               「安装 Microsoft Authenticator」
#               ★ 有一个 `<button class="ms-Link">跳过设置</button>`
#
#  → **「跳过」在第二屏，第一屏上没有**。
#    这就是为什么只看第一屏会得出「没有跳过入口」的结论。
#
#  实测（2026-09-23）：
#    · 点完这两下，页面立刻回到 `beijing101.sharepoint.com`
#    · 然后在那个页面上 fetch，直接拿到 `%PDF-` 4,058,844 字节
#    · **而且之后不再需要点** —— 再跑一次时第一屏就是 SharePoint，
#      直接出 PDF（说明「跳过」这个选择被记住了）
#
#  ★ 关于「该不该自动点」
#    这是**拒绝一个可选的**安全信息注册，不是绕过身份验证：
#      · 账号本人已经登录（会话里就是他）
#      · 我们只点「跳过设置 / 暂不设置」这类「以后再说」的入口
#      · 不填任何假的验证方式、不改任何账号设置
#    学校若把它设成强制（页面上不放跳过），这里就如实报告、不硬闯。
# ══════════════════════════════════════════════════════════

#  「下一步」的各种措辞
MFA_STEP1_WORDS = ("下一步", "下一个", "Next", "继续", "Continue")

#  「跳过」的各种措辞（只认这些明确的「以后再说」）
MFA_SKIP_WORDS = (
    "跳过设置", "跳过", "暂不设置", "稍后设置", "以后再说", "稍后再说",
    "Skip for now", "Skip setup", "Skip", "Set up later", "Do it later",
    "Not now", "Remind me later",
)

#  在哪一屏点哪个词
MFA_SCREENS = (
    ("login.microsoftonline.com", "下一步", MFA_STEP1_WORDS),
    ("mysignins.microsoft.com", "跳过设置", MFA_SKIP_WORDS),
)

#  点一个文字完全匹配的元素。
#  ★ 必须读 `.value` —— `<input type=submit value="下一步">` 的文字
#    在 value 里，`innerText` / `textContent` **都是空的**。
#    （这一条踩过两次：只读 innerText 的探针找不到主按钮。）
_CLICK_MATCH_JS = r"""
(want) => {
  const roots = [document];
  try {
    document.querySelectorAll('iframe').forEach(f => {
      try { if (f.contentDocument) roots.push(f.contentDocument); }
      catch (e) {}
    });
  } catch (e) {}
  const norm = s => String(s || '').replace(/\s+/g, ' ').trim();
  const w = norm(want);
  if (!w) return {ok: false};
  for (const doc of roots) {
    let all;
    try { all = doc.querySelectorAll('*'); } catch (e) { continue; }
    for (const el of all) {
      const tag = el.tagName;
      if (!['BUTTON', 'A', 'INPUT', 'SPAN', 'DIV'].includes(tag)) continue;
      const t = norm((el.value || '') + ' ' + (el.innerText || '')
                     + ' ' + (el.getAttribute('aria-label') || ''));
      if (t !== w) continue;
      let r;
      try { r = el.getBoundingClientRect(); } catch (e) { continue; }
      if (r.width < 1 || r.height < 1) continue;
      try { el.scrollIntoView({block: 'center'}); } catch (e) {}
      const r2 = el.getBoundingClientRect();
      const cx = r2.x + r2.width / 2, cy = r2.y + r2.height / 2;
      const mk = type => new MouseEvent(type, {
        bubbles: true, cancelable: true, view: window, button: 0,
        buttons: 1, clientX: cx, clientY: cy,
      });
      //  Fluent / React 只认完整的指针事件序列，单发 click 会被忽略
      ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']
        .forEach(tp => { try { el.dispatchEvent(mk(tp)); } catch (e) {} });
      try { el.click(); } catch (e) {}
      return {ok: true, tag: tag, id: el.id || '', text: t.slice(0, 40)};
    }
  }
  return {ok: false};
}
"""

_MFA_STATE_JS = r"""
() => ({
  host: location.host,
  href: String(location.href).slice(0, 140),
  title: (document.title || '').slice(0, 60),
  ready: document.readyState,
  //  这一屏上所有短文字的可点元素（供判断走到了哪一步）
  btns: (() => {
    const out = [];
    document.querySelectorAll(
      'button, input[type=submit], a, [role=button]').forEach(el => {
      const t = String((el.value || '') + (el.innerText || ''))
                  .replace(/\s+/g, ' ').trim();
      if (t && t.length < 40) out.push(t);
    });
    return [...new Set(out)].slice(0, 16);
  })(),
})
"""


def mfa_state(page) -> dict:
    """读当前页面的状态（含这一屏上的短按钮文字）。"""
    try:
        return page.eval(_MFA_STATE_JS) or {}
    except Exception:
        return {}


def _click_match(page, word: str) -> bool:
    try:
        r = page.call(_CLICK_MATCH_JS, word) or {}
    except Exception:
        return False
    return bool(r.get("ok"))


def _wait_click(page, words, host_part: str, *, timeout: float = 40.0,
                quiet: bool = False) -> dict:
    """等目标屏渲染出来，再点那几个词里出现的第一个。

    ★ 别等「域名变了」就动手 —— 域名一变 SPA 还没渲染完，
      按钮还不存在，点了等于没点（实测踩到：第 2 屏标题都还是空的）。
      要等的是**目标按钮出现**。
    """
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = mfa_state(page)
        if host_part in str(st.get("host") or ""):
            for w in words:
                if _click_match(page, w):
                    if not quiet:
                        print(f"    ★ 点了 {w!r}")
                    return {"ok": True, "word": w,
                            "host": str(st.get("host") or "")}
        time.sleep(1.5)
    return {"ok": False, "host": str(mfa_state(page).get("host") or "")}


def skip_mfa(page, *, verbose: bool = False, timeout: float = 60.0) -> dict:
    """如果当前卡在 MFA 注册提示上，点掉它。

    返回 ``{ok, skipped, note?}``：
      · ``skipped=True``  —— 点掉了（第一次会走两屏）
      · ``skipped=False`` —— 当前不在 MFA 页上（**大多数时候是这种**，
        因为「跳过」被记住之后就不再出现了）
    """
    st = mfa_state(page)
    host = str(st.get("host") or "")

    if not any(h in host for h, _, _ in MFA_SCREENS):
        return {"ok": True, "skipped": False,
                "note": "当前不在二次验证页上"}

    if verbose:
        print(f"    落在 {host!r}，试着跳过…")

    t0 = time.time()
    skipped_once = False

    #  最多两屏。屏与屏之间的判断靠 host —— 但点完之后 host 会变，
    #  所以用 while 让它自然推进。
    for _ in range(2):
        if time.time() - t0 > timeout:
            break
        st = mfa_state(page)
        host = str(st.get("host") or "")
        matched = None
        for part, _label, words in MFA_SCREENS:
            if part in host:
                matched = (part, words)
                break
        if matched is None:
            #  已经不在 MFA 页上了 → 成了
            break
        part, words = matched
        r = _wait_click(page, words, part, timeout=30, quiet=not verbose)
        if not r.get("ok"):
            #  这一屏上没找到跳过入口 → 说明学校要求强制设置
            if part == "mysignins.microsoft.com":
                return {
                    "ok": False, "skipped": skipped_once,
                    "note": "这一屏上没有「跳过设置」——"
                            "学校可能要求必须完成安全信息设置，"
                            "这种情况需要你自己在浏览器里设置一次",
                }
            return {"ok": False, "skipped": skipped_once,
                    "note": f"在 {part} 上没找到下一步按钮"}
        skipped_once = True
        #  等它跳到下一屏（等画面真的换了，别干等固定秒数）
        for _ in range(20):
            time.sleep(1.0)
            h = str(mfa_state(page).get("host") or "")
            if "sharepoint" in h or h != host:
                break

    final_host = str(mfa_state(page).get("host") or "")
    back = "sharepoint" in final_host
    if verbose:
        print(f"    跳过之后落在 {final_host!r}")
    return {
        "ok": back or skipped_once,
        "skipped": skipped_once,
        "host": final_host,
        "note": "" if back else f"点完了但没回到 SharePoint（在 {final_host}）",
    }


def goto_personal(page, rel_path: str = "",
                  host: str = PERSONAL_HOSTS[0], timeout: float = 40.0) -> str:
    """把页面导航到个人盘，返回最终落在哪个主机名。

    ★★ 这一步**不能省**：列目录是页面内 `fetch`，受同源策略约束。
      如果页面还停在站点库（`beijing101.sharepoint.com`）或者更糟 ——
      停在 `login.microsoftonline.com`，那么对个人盘的 fetch 会被 CORS 拦下，
      报一个很难懂的 `TypeError: Failed to fetch`。

      实测踩到过：站点库链接失败后直接在原地调 list_files，
      两次都是「列目录 HTTP None」—— 因为 origin 根本不对。

    落在个人盘后用 `onedrive.aspx?id=` 打开目标目录 ——
    这会让 SPA 把该目录的上下文加载好（cookie / 请求头都齐）。
    """
    if rel_path:
        from urllib.parse import quote as _q
        url = (f"{host}/_layouts/15/onedrive.aspx"
               f"?id={_q(rel_path, safe='')}&ga=1")
    else:
        url = f"{host}/"

    try:
        page.goto(url, timeout=60000)
    except Exception as e:
        log.debug("导航个人盘抛错，未必失败：%s", str(e)[:90])

    #  ★ 不能只看 host 就返回 ——
    #    sharepoint 的导航是**多跳**的（先到登录域、再到目标站点、
    #    最后 SPA 自己再跳一次）。
    #    host 一旦变成 my.sharepoint 就返回的话，页面可能还在跳，
    #    此时发 fetch 会因为「请求头/会话上下文还没就绪」
    #    返回 **403 UnauthorizedAccess**（实测踩到过）。
    #
    #    所以要等到「标题稳定 + 不是登录页」才算就绪。
    step = 2.0
    waited = 0.0
    host_now = ""
    last_title = ""
    stable = 0
    while waited < timeout:
        time.sleep(step)
        waited += step
        try:
            host_now = str(page.eval("location.host") or "")
            title = str(page.eval("document.title") or "").strip()
        except Exception:
            host_now, title = "", ""

        if host_now and any(x in host_now.lower() for x in LOGIN_HOSTS):
            log.info("个人盘也要求登录（%s）", host_now)
            return host_now

        if "my.sharepoint" in host_now and title:
            if title == last_title:
                stable += 1
                #  连续两次标题不变 → 认为导航结束了
                if stable >= 2:
                    return host_now
            else:
                stable = 0
            last_title = title
    return host_now


def list_files(page, rel_path: str,
               host: str = PERSONAL_HOSTS[0]) -> dict:
    """列一个 SharePoint 目录下的文件。返回 {ok, files:[{...}], note?}。

    走 `_api/web/GetFolderByServerRelativeUrl('<编码路径>')/Files`。

    ★ 千万别加 `$select` —— 实测加了就 403。

    每项给：`name` / `unique_id` / `size` / `server_url` / `created`
    """
    from urllib.parse import quote as _q
    api = (f"{host}/_api/web/GetFolderByServerRelativeUrl("
           f"'{_q(rel_path, safe='')}')/Files")
    try:
        r = page.call(LIST_FILES_JS, api) or {}
    except Exception as e:
        return {"ok": False, "files": [],
                "note": f"列目录抛错：{str(e)[:120]}"}

    if r.get("err"):
        return {"ok": False, "files": [],
                "note": f"列目录被拦下：{str(r['err'])[:110]}"
                        f"（多半是当前页面不在这个域名下）"}

    st = r.get("status")
    if st != 200:
        return {"ok": False, "files": [],
                "note": f"列目录 HTTP {st}：{str(r.get('text'))[:140]}"}
    try:
        d = json.loads(str(r.get("text") or "{}"))
    except Exception as e:
        return {"ok": False, "files": [], "note": f"列目录响应不是 JSON：{e}"}

    out = []
    for it in (d.get("value") or []):
        if not isinstance(it, dict):
            continue
        nm = str(it.get("Name") or "")
        uid = str(it.get("UniqueId") or "").strip("{}")
        if not nm or not GUID_RE.match(uid):
            continue
        try:
            sz = int(it.get("Length") or 0)
        except Exception:
            sz = 0
        out.append({
            "name": nm, "unique_id": uid, "size": sz,
            "server_url": str(it.get("ServerRelativeUrl") or ""),
            "created": str(it.get("TimeCreated") or ""),
            "modified": str(it.get("TimeLastModified") or ""),
        })
    return {"ok": True, "files": out, "note": ""}


GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                     r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def fetch_by_unique_id(page, unique_id: str, *,
                       host: str = PERSONAL_HOSTS[0],
                       verbose: bool = False) -> dict:
    """按 UniqueId 取字节。

    ★ 端点来历：读预览页的 `performance.getEntriesByType('resource')`，
      看到**页面自己**用的就是 `_layouts/15/download.aspx?UniqueId=<GUID>`，
      返回 4 MB 整的 PDF。比手拼 REST 端点靠谱得多。

    两个主机名都试（个人盘在 `-my.`，根站点也认这个接口）。
    """
    uid = str(unique_id or "").strip("{}")
    if not GUID_RE.match(uid):
        return {"ok": False, "note": f"UniqueId 格式不对：{uid[:50]!r}"}

    urls: list[str] = []
    for h in (host, "https://beijing101.sharepoint.com"):
        urls.append(f"{h}/_layouts/15/download.aspx?UniqueId={uid}")
        urls.append(f"{h}/_layouts/15/download.aspx"
                    f"?UniqueId={uid}&Translate=false")

    #  ★ 同样受同源约束 —— 页面得在 sharepoint 域名下。
    #    若当前不在（例如刚被踢到登录页），把它挪回个人盘。
    try:
        host_now = str(page.eval("location.host") or "")
    except Exception:
        host_now = ""
    if "sharepoint" not in host_now.lower():
        log.debug("取字节前先回到个人盘（当前 %s）", host_now)
        goto_personal(page, "")

    try:
        results = page.call(FETCH_JS, urls) or []
    except Exception as e:
        return {"ok": False, "note": f"取字节失败：{str(e)[:140]}"}

    for r in results:
        if r.get("err"):
            if verbose:
                print(f"    x {str(r['url'])[:80]}  {r['err'][:70]}")
            continue
        if verbose:
            print(f"    {r.get('status')} {r.get('magic')!r} "
                  f"{r.get('len', 0):>11,}B  {str(r['url'])[:70]}")
        if r.get("b64"):
            raw = base64.b64decode(r["b64"])
            if len(raw) > MAX_MB * 1024 * 1024:
                return {"ok": False, "note": f"文件过大（{len(raw)/1e6:.1f} MB）"}
            return {"ok": True, "data": raw, "size": len(raw),
                    "url": r.get("url")}
    return {"ok": False, "note": "download.aspx 没返回 %PDF-"}


def find_in_personal(page, filename: str, *, site_path: str = "",
                     verbose: bool = False) -> dict:
    """在个人盘里按文件名找一份文件。返回 {ok, item?, dir?, note?}。

    先列根目录，找不到再列候选子目录（从 site_path 里猜的）。

    ★ 为什么值得做这一步：
      EC 频道里**最新**的几份往往用站点库链接发出来（跳 MFA），
      但同一批文件通常在老师的个人盘上也有一份 ——
      个人盘这条路**能自动化**。能捞到就省掉用户一次手动登录。
    """
    want = re.sub(r"\s+", "", (filename or "")).lower()
    if not want:
        return {"ok": False, "note": "文件名为空"}

    roots = personal_roots(page, site_path)

    #  ★ 先把页面挪到个人盘 —— 不然下面的 fetch 会被同源策略拦掉
    landed = goto_personal(page, roots[0])
    if "my.sharepoint" not in landed:
        return {"ok": False, "kind": "need_login", "host": landed,
                "note": f"个人盘也需要登录（落在 {landed or '未知'}）"}

    for rel in roots:
        r = list_files(page, rel)
        if not r.get("ok"):
            if verbose:
                print(f"    列 {rel[:70]} 失败：{r.get('note')}")
            continue
        files = r.get("files") or []
        if verbose:
            print(f"    {rel[:70]} → {len(files)} 个文件")
        #  先精确（忽略空白与大小写），再宽松（去掉扩展名包含）
        for it in files:
            nm = re.sub(r"\s+", "", it["name"]).lower()
            if nm == want:
                return {"ok": True, "item": it, "dir": rel}
        stem = want[:-4] if want.endswith(".pdf") else want
        for it in files:
            nm = re.sub(r"\s+", "", it["name"]).lower()
            if stem and stem in nm:
                return {"ok": True, "item": it, "dir": rel}
    return {"ok": False, "note": f"个人盘里没有 {filename!r}"}


def fetch_document(page, link: str, *, site_path: str = "",
                   allow_personal: bool = False,
                   verbose: bool = False) -> dict:
    """取一个 SharePoint 文件的字节。

    流程：按分享链接的方式取（导航到预览页 → 页面内 fetch）。

    ── 关于 `allow_personal`（默认关）──

      本可以再加一条兜底：站点库链接失败时，去**个人盘**按文件名找一份。
      代码写好了（`find_in_personal` / `fetch_by_unique_id`），
      实测也**曾经**跑通过（个体例：列到 5 个文件、能按 UniqueId 取字节）。

      但正式接进来之后**不可靠**，原因查清了：

        · 列目录接口 `_api/web/GetFolderByServerRelativeUrl(…)/Files`
          时而 200、时而 **403**，而且**一旦 403 就会持续 403**，
          重新导航也救不回来，要等会话自己恢复。
          触发条件像是「这次会话里先走过一次分享链接 / 站点库跳转」。
        · 更要紧的是：**它救不了真正失败的那两份。**

      实测个人盘 `Documents` 里只有 5 个文件：

          EC Roster 9.11 / 9.14 / 9.15 / 9.17 / 9.18.pdf
          子目录 0 个

      而失败的恰恰是 **9.21 / 9.22** —— 这两份**不在个人盘上**。
      所以走这条路注定扑空，只会让每次下载都多花十几秒去失败一次。

      → 默认关掉。真要用的时候（比如换个老师、文件挪到个人盘了）
        传 `allow_personal=True`，那条链路是完整的。
    """
    r = fetch_bytes(page, link, verbose=verbose)
    if r.get("ok") or not allow_personal:
        return r

    if r.get("kind") in ("dead_page",):
        return r

    name = ""
    try:
        tail = urlparse(str(link)).path.rsplit("/", 1)[-1]
        from urllib.parse import unquote as _uq
        name = _uq(tail)
    except Exception:
        name = ""
    if not is_pdf_name(name):
        m = re.search(r"/([^/]+\.pdf)", str(link), re.I)
        if m:
            name = m.group(1)
    if not is_pdf_name(name):
        return r

    if verbose:
        print(f"    分享链接失败（{r.get('kind') or '未知'}）→ 去个人盘找 {name!r}")

    found = find_in_personal(page, name, site_path=site_path, verbose=verbose)
    if not found.get("ok"):
        return dict(r, note=(r.get("note") or "") +
                    "；个人盘里也没有同名文件")
    it = found["item"]
    got = fetch_by_unique_id(page, it["unique_id"], verbose=verbose)
    if not got.get("ok"):
        return dict(r, note=(r.get("note") or "") +
                    f"；个人盘里找到了但取字节失败：{got.get('note')}")
    return {"ok": True, "data": got["data"], "name": it["name"],
            "url": got.get("url"), "size": got.get("size"),
            "via": "personal"}


def _self_test() -> int:
    """自测：只测解析，不联网。"""
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
    print("  SharePoint 下载模块自测（离线部分）")
    print("=" * 62)

    real = ("EC Roster 9.18.pdf\n"
            "https://beijing101-my.sharepoint.com/:b:/g/personal/"
            "emerson_miller_beijing101id_com/IQBNYX8JVPurSrAoGx6TL_r0AaxgWq"
            "E1tCwo5Vz5_PqbGLg?e=g8d103")
    g = link_from_label(real)
    chk(g and g["name"] == "EC Roster 9.18.pdf", "拆出文件名（真实 label）")
    chk(g and g["url"].startswith("https://beijing101-my.sharepoint.com/:b:"),
        "拆出链接（真实 label）")
    chk(g and "\n" not in g["url"] and " " not in g["url"],
        "链接里没有残留空白")

    #  真机上的折行形态（长 URL 会被折开）
    wrapped = ("9.22 2.pdf\nhttps://beijing101.sharepoint.com/sites/"
               "Beijing101HighSchool/Shared%20Documents/ENGLISH%20CORNER%20"
               "ROSTER/\nEC%20Roster%209.22%202.pdf")
    g2 = link_from_label(wrapped)
    chk(g2 and " " not in g2["url"], "折行的链接被接回一行")

    #  名字后面跟一堆参数名（Teams 有时这么给）
    messy = "EC Roster 9.17.pdf\nEC Roster 9.17.pdf\nhttps://x.sharepoint.com/:b:/a"
    g3 = link_from_label(messy)
    chk(g3 and g3["name"] == "EC Roster 9.17.pdf", "从多个候选里挑出干净名字")

    #  没有链接的 label 要返回 None（不能瞎猜）
    chk(link_from_label("EC Roster 9.11.pdf") is None, "无链接 → None")
    chk(link_from_label("") is None, "空串 → None")
    chk(link_from_label("随便一段中文") is None, "纯文本 → None")

    #  候选地址
    link = ("https://beijing101-my.sharepoint.com/:b:/g/personal/x/"
            "IQBxxx?e=abc")
    probe = {"href": "https://beijing101-my.sharepoint.com/personal/x/"
                     "_layouts/15/onedrive.aspx?id=%2Fpersonal%2Fx%2FDocuments"
                     "%2FEC%20Roster%209.18%2Epdf&ga=1",
             "ids": {"id": "/personal/x/Documents/EC Roster 9.18.pdf"}}
    cs = candidates(link, probe)
    chk(len(cs) >= 5, f"候选地址够多（{len(cs)} 个）")
    chk(cs[0] == link + "&download=1", "第 1 个是「原链接 + download=1」")
    chk(any("download.aspx" in c for c in cs), "含 download.aspx 形态")
    chk(any("$value" in c for c in cs), "含 REST $value 形态")
    chk(any("drive/root:" in c for c in cs), "含 v2.0 drive 形态")
    chk(all(c.startswith("http") for c in cs), "全部是绝对地址")
    #  id 里的空格必须编码，否则请求会 400
    chk(all(" " not in c for c in cs), "地址里没有裸空格（已编码）")
    chk(len(cs) == len(set(cs)), "候选去重")

    #  没有 probe 也要能拼出至少一条
    chk(len(candidates(link)) >= 1, "无 probe 时仍能给出候选")

    #  ── 个人盘目录推算 ──
    site = ("/sites/Beijing101HighSchool/Shared Documents/"
            "ENGLISH CORNER ROSTER/EC Roster 9.22 2.pdf")
    roots = personal_roots(None, site)
    chk(len(roots) >= 2, f"能从站点库路径推出个人盘目录（{len(roots)} 个）")
    chk(roots[0].endswith("/Documents"), "第一个是 Documents 根")
    chk(any("ENGLISH CORNER ROSTER" in r for r in roots),
        "推得出同名子目录")
    chk(all(r.startswith("/personal/") for r in roots),
        "全是以 /personal/ 开头")
    chk(len(roots) == len(set(roots)), "目录去重")
    #  没有线索时至少要有根目录
    chk(personal_roots(None, "") == [roots[0]],
        "没线索 → 只给 Documents 根")

    #  ── UniqueId 校验 ──
    chk(GUID_RE.match("097f614d-fb54-4aab-b028-1b1e932ffaf4") is not None,
        "接受真实 UniqueId")
    chk(GUID_RE.match("097F614D-FB54-4AAB-B028-1B1E932FFAF4") is not None,
        "接受大写 UniqueId")
    chk(GUID_RE.match("{097f614d-fb54-4aab-b028-1b1e932ffaf4}") is None,
        "带花括号的要先剥掉（正则本身不收）")
    chk(GUID_RE.match("not-a-guid") is None, "拒绝非 GUID")
    chk(GUID_RE.match("") is None, "拒绝空串")

    #  ── 登录页识别 ──
    chk(looks_like_login("login.microsoftonline.com", "", ""),
        "识别 login.microsoftonline.com")
    chk(looks_like_login("", "登录到您的帐户",
                         "ryan@x.com 让我们来保护帐户安全 密码"),
        "识别「登录到您的帐户」")
    chk(not looks_like_login("beijing101-my.sharepoint.com",
                             "EC Roster 9.18.pdf", ""),
        "预览页不算登录页")
    chk(not looks_like_login("", "登录", ""),
        "只写「登录」而没正文 → 不算（防误判）")

    print("-" * 62)
    print(f"  通过 {ok} / 失败 {fail}")
    return 1 if fail else 0


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(_self_test())
