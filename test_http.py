"""测试：纯 Python HTTP 能否完成全部抓取（不需要浏览器）。

如果能，架构可以明显简化：
    - 登录：POST /sessions
    - 会话：保存 _managebac_session Cookie（30 天有效）
    - 抓取：直接 HTTP GET 页面 HTML

好处：速度较快、稳（不依赖浏览器）、开机预热开销很小。
"""
from __future__ import annotations

import http.cookiejar
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from app import config  # noqa: E402

BASE = config.BASE_URL
SESSION_FILE = config.DATA_DIR / "session_cookies.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36 Edg/153.0.0.0"
)

LOG: list[str] = []


def say(s: str = "") -> None:
    LOG.append(s)
    print(s, flush=True)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


# ---------------------------------------------------------------- 会话

def new_opener() -> tuple[urllib.request.OpenerDirector, http.cookiejar.CookieJar]:
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj), NoRedirect()
    )
    op.addheaders = [  # type: ignore[attr-defined]
        ("User-Agent", UA),
        ("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
        ("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8"),
        ("Accept-Encoding", "identity"),
    ]
    return op, cj


def get(op, url: str, referer: str | None = None) -> tuple[int, dict, str]:
    r = urllib.request.Request(url)
    if referer:
        r.add_header("Referer", referer)
    try:
        resp = op.open(r, timeout=40)
        return resp.status, dict(resp.headers), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return 0, {}, f"<异常> {e!r}"


def save_cookies(cj: http.cookiejar.CookieJar) -> None:
    data = [
        {"name": c.name, "value": c.value, "domain": c.domain,
         "path": c.path, "expires": c.expires, "secure": c.secure}
        for c in cj
    ]
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                            encoding="utf-8")


def load_cookies() -> bool:
    if not SESSION_FILE.exists():
        return False
    try:
        raw = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    except Exception:
        return False
    # 装进 opener
    return bool(raw)


def restore(cj: http.cookiejar.CookieJar) -> int:
    if not SESSION_FILE.exists():
        return 0
    try:
        raw = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    except Exception:
        return 0
    n = 0
    for d in raw:
        cj.set_cookie(http.cookiejar.Cookie(version=0, name=d["name"], value=d["value"], port=None,
            port_specified=False, domain=d["domain"], domain_specified=True,
            domain_initial_dot=d["domain"].startswith("."),
            path=d.get("path", "/"), path_specified=True,
            secure=bool(d.get("secure")), expires=d.get("expires"),
            discard=False, comment=None, comment_url=None, rest={},
        ))
        n += 1
    return n


# ---------------------------------------------------------------- 登录

def login(op, cj, login: str, password: str) -> tuple[bool, str]:
    st, _, html = get(op, f"{BASE}/login")
    if st != 200:
        return False, f"打开登录页失败（{st}）"
    m = re.search(r'name="authenticity_token" value="([^"]+)"', html)
    if not m:
        return False, "找不到 CSRF token"
    token = m.group(1)

    body = urllib.parse.urlencode({
        "authenticity_token": token, "login": login,
        "password": password, "remember_me": "1", "commit": "Sign in",
    }).encode()
    r = urllib.request.Request(f"{BASE}/sessions", data=body)
    r.add_header("Content-Type", "application/x-www-form-urlencoded")
    r.add_header("Referer", f"{BASE}/login")
    r.add_header("Origin", BASE)

    try:
        resp = op.open(r, timeout=40)
        st, hd, txt = resp.status, dict(resp.headers), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        st, hd, txt = e.code, dict(e.headers), e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"

    if st in (301, 302, 303, 307, 308):
        loc = hd.get("Location") or hd.get("location") or ""
        return True, f"登录成功 → {loc}"

    # 失败：抠出原因
    mm = re.search(r"alert-content'>([^<]+)", txt)
    detail = mm.group(1).strip() if mm else "服务端未给出原因"
    return False, f"登录被拒（{st}）：{detail[:200]}"


def main() -> int:
    cred = json.loads((ROOT / "credentials.json").read_text(encoding="utf-8"))["_managebac"]
    login_id, password = cred["login"], cred["password"]

    say("=" * 72)
    say("  纯 Python HTTP 抓取可行性测试")
    say("=" * 72)
    say(f"账号: {login_id}")

    op, cj = new_opener()

    # 1) 先试恢复旧会话
    n = restore(cj)
    if n:
        say(f"\n[1] 尝试复用已保存的会话（{n} 个 Cookie）……")
        st, _, html = get(op, f"{BASE}/student/home")
        say(f"    GET /student/home → {st}")
        if st == 200 and "/login" not in html[:2000] and "session_form" not in html:
            say("    [OK] 旧会话仍然有效！")
        else:
            say("    会话已失效，需要重新登录")
            n = 0
    else:
        say("\n[1] 没有已保存的会话")

    # 2) 登录
    if not n:
        say("\n[2] 用凭据登录……")
        ok, msg = login(op, cj, login_id, password)
        say(f"    {'[OK]' if ok else '[X] '} {msg}")
        if not ok:
            (ROOT / "http_test.log").write_text("\n".join(LOG), encoding="utf-8")
            return 1
        save_cookies(cj)
        say(f"    已保存会话 → {SESSION_FILE.name}")

    # 3) 抓首页
    say("\n[3] 抓 /student/home ……")
    st, _, html = get(op, f"{BASE}/student/home")
    say(f"    状态 {st}，{len(html)} 字节")
    if st != 200 or "session_form" in html:
        say("    [X] 拿不到学生主页")
        (ROOT / "http_test.log").write_text("\n".join(LOG), encoding="utf-8")
        return 1

    (config.DATA_DIR / "http_home.html").write_text(html, encoding="utf-8")
    say(f"    已存 → data/http_home.html")

    # 4) 找课程链接
    links = re.findall(r'href="(/student/classes/\d+[^"]*)"', html)
    uniq = sorted(set(links))
    say(f"\n[4] 首页找到 {len(uniq)} 个课程链接：")
    for u in uniq[:12]:
        say(f"    {u}")

    # 5) 抓一个课程页试试
    if uniq:
        cid = re.match(r"/student/classes/(\d+)", uniq[0]).group(1)  # type: ignore[union-attr]
        say(f"\n[5] 抓课程 {cid} 的 /core_tasks ……")
        st2, _, html2 = get(op, f"{BASE}/student/classes/{cid}/core_tasks",
                            referer=f"{BASE}/student/home")
        say(f"    状态 {st2}，{len(html2)} 字节")
        (config.DATA_DIR / f"http_course_{cid}.html").write_text(html2, encoding="utf-8")

        cards = len(re.findall(r"fusion-card-item", html2))
        cells = len(re.findall(r"assessment-cell", html2))
        say(f"    任务卡片(fusion-card-item): {cards}")
        say(f"    成绩单元格(assessment-cell): {cells}")
        if cards:
            say("    [OK] 课程页结构可解析！")

    say("\n" + "=" * 72)
    say("  结论")
    say("=" * 72)
    say("  纯 HTTP 可行 → 可去掉浏览器依赖，预热在几秒内完成")
    (ROOT / "http_test.log").write_text("\n".join(LOG) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
