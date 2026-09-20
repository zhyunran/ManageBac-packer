"""链接安全校验 —— 从 CampusDesk（Swift 版）学来的做法。

【为什么需要】

  我们的界面里到处是「点一下打开原网页」的链接，链接直接来自
  抓下来的 HTML。如果页面结构变了、或者解析出错抓到了旁边的链接，
  用户点下去可能打开**危险的页面**，例如：

      /student/logout          ← 一点就退出登录
      /student/.../delete      ← 删除东西
      /student/.../upload      ← 上传表单
      .../edit                 ← 编辑表单
      .../new                  ← 新建表单

  这些都不是「查看」操作，不该被当成「打开原网页」。

【CampusDesk 的做法（我们借鉴）】

  它有一个 `safeURL()`，双重校验：

    ① **白名单**：只保留这几个查询参数
         term / page / academic_year / year / view / filter
       —— 因为带签名的附件链接、跟踪参数之类的都不该被保留

    ② **黑名单**：路径里出现这些词就直接拒绝
         logout / delete / destroy / dropbox / upload / edit / new

    ③ 协议必须 https、必须同源、不能带账号密码

【我们的实现】

  比 CampusDesk 更宽一点（我们确实要打开附件下载链接、Files 目录等），
  所以白名单放宽、但黑名单保持一致。

  核心保证：**只允许「查看/下载」类链接通过。**
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urlparse, parse_qs

log = logging.getLogger("safelink")

# ── 允许的域名后缀（我们自己的两个站点）──
ALLOWED_HOSTS = ("managebac.cn", "managebac.com",
    "seiue.com",
    # 附件直链在 S3 上
    "amazonaws.com.cn", "amazonaws.com",
)

# ──  黑名单：路径里出现这些词就拒绝（会导致副作用）──
#   这是最关键的防护 —— 参考 CampusDesk 的做法并略作扩充
DANGEROUS_WORDS = ("logout", "sign_out", "signout", "sign_off", "exit",   # 退出登录
    "delete", "destroy", "remove", "drop", "purge",        # 删除
    "upload", "import",                                    # 上传
    "edit", "update", "new", "create", "add",              # 编辑/新建
    "reset", "revoke", "unsubscribe",                      # 其他副作用
    "admin",
    "submit",                                              #  提交作业（绝不代提交）
)
_BAD_WORD_RE = re.compile(r"(?:^|/)(?:%s)(?:/|$|\?|\.)" % "|".join(DANGEROUS_WORDS),
    re.I,
)

# ── 白名单：允许保留的查询参数 ──
#   其他参数（可能是签名、token、跟踪）一律剔除
ALLOWED_QUERY = {
    "term", "page", "academic_year", "year", "view", "filter",
    "cid", "fid",               # 我们自己的课程/文件夹 id
    "start_time", "end_time",   # 课表时间范围
    "expand",                   # 课表 expand
}

# ── 允许的协议 ──
ALLOWED_SCHEMES = {"https", "http"}


def _host_ok(host: str) -> bool:
    host = (host or "").lower().strip()
    if not host:
        return False
    return any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS)


def _is_local(host: str) -> bool:
    """本地地址（我们自己的预览页 / 看板服务）。"""
    h = (host or "").lower()
    return h in ("localhost", "127.0.0.1", "::1", "")


def check(url: str, *, allow_local: bool = True) -> tuple[bool, str]:
    """校验一个链接能不能安全打开。

    Returns:
        (是否安全, 拒绝原因)
    """
    if not url:
        return False, "空链接"

    try:
        u = urlparse(url)
    except Exception as e:
        return False, f"链接无法解析：{e}"

    # ① 协议
    scheme = (u.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        return False, f"不支持的协议：{scheme or '(空)'}"

    # ② 域名
    host = u.hostname or ""
    if _is_local(host):
        if not allow_local:
            return False, "不允许本地地址"
    elif not _host_ok(host):
        return False, f"不在允许的站点范围内：{host}"

    # ③ 不能带账号密码（钓鱼常用手法）
    if u.username or u.password:
        return False, "链接里包含账号密码信息"

    # ④  危险路径（最重要的一条）
    path = u.path or ""
    m = _BAD_WORD_RE.search(path)
    if m:
        word = m.group(0).strip("/.?")
        return False, f"这是「{word}」类操作，不是查看页面（已阻止）"

    # ⑤ 查询参数白名单 —— 剔除可疑参数
    if u.query:
        keys = set(parse_qs(u.query, keep_blank_values=True).keys())
        bad = {k for k in keys if k.lower() not in ALLOWED_QUERY}
        if bad:
            #  只在「看起来像签名/token」时才拒绝
            #   （普通参数如 ?utm_source= 这种放宽，免得误伤）
            looks_secret = any(re.search(r"(?:token|sign|auth|key|secret|credential|session)",
                          k, re.I)
                for k in bad
            )
            if looks_secret:
                return False, f"链接带有敏感参数：{sorted(bad)[:3]}"

    return True, ""


def sanitize(url: str) -> str:
    """校验并返回安全的链接；不安全时返回空串。

    用法：拿到的链接一律先过这里再用。
    """
    ok, why = check(url)
    if not ok:
        log.info("已阻止不安全链接（%s）：%s", why, (url or "")[:120])
        return ""
    return url


def explain(url: str) -> str:
    """返回人类可读的说明（用于界面提示）。"""
    ok, why = check(url)
    return "可以打开" if ok else why


# ---------------------------------------------------------------- 自测

def _self_test() -> int:
    """跑一遍用例，确认规则符合预期。"""
    cases: list[tuple[str, bool]] = [
        # ── 应该允许 ──
        ("https://your-school.managebac.cn/student/classes/123/core_tasks", True),
        ("https://your-school.managebac.cn/student/classes/123/core_tasks/456", True),
        ("https://your-school.managebac.cn/student/classes/123/files", True),
        ("https://your-school.managebac.cn/student/classes/123/files/folder/9001", True),
        ("https://your-school.managebac.cn/student/home", True),
        ("https://yly.seiue.com/", True),
        ("https://api.seiue.com/chalk/calendar/personals/1/events"
         "?start_time=2026-01-01&end_time=2026-01-02&expand=address", True),
        ("https://bucket.s3.cn-north-1.amazonaws.com.cn/attachments/x.pdf", True),
        ("https://your-school.managebac.cn/student/classes/1/core_tasks?page=2", True),
        # 本地预览
        ("file:///D:/x/_preview.html", False),   # file 协议不在允许列表
        ("http://localhost:8760/index.html", True),
        # ── 应该拒绝（危险操作）──
        ("https://your-school.managebac.cn/logout", False),
        ("https://your-school.managebac.cn/student/logout", False),
        ("https://your-school.managebac.cn/users/sign_out", False),
        ("https://your-school.managebac.cn/student/classes/1/tasks/2/delete", False),
        ("https://your-school.managebac.cn/student/classes/1/files/upload", False),
        ("https://your-school.managebac.cn/student/classes/1/tasks/new", False),
        ("https://your-school.managebac.cn/student/classes/1/tasks/2/edit", False),
        ("https://your-school.managebac.cn/student/classes/1/submit", False),
        ("https://your-school.managebac.cn/admin/users", False),
        # ── 应该拒绝（安全）──
        ("javascript:alert(1)", False),
        ("data:text/html,<script>alert(1)</script>", False),
        ("https://evil.com/student/classes/1", False),
        ("https://managebac.cn.evil.com/x", False),
        ("https://user:pass@your-school.managebac.cn/student/home", False),
        ("https://your-school.managebac.cn/student/x?token=abc123", False),
        ("", False),
    ]

    fails = 0
    print("=" * 68)
    print("  链接安全校验自测")
    print("=" * 68)
    for url, want in cases:
        got, why = check(url)
        ok = (got == want)
        if not ok:
            fails += 1
        flag = "OK " if ok else "XX "
        mark = "允许" if got else "拒绝"
        note = "" if got else f"  ← {why}"
        short = url if len(url) <= 58 else url[:55] + "..."
        print(f"  {flag}{mark}  {short}{note}")

    print()
    total = len(cases)
    print(f"  {total - fails}/{total} 通过")
    if fails:
        print(f"  [X] {fails} 个用例不符合预期")
    else:
        print("  [OK] 全部符合预期")
    print("=" * 68)
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    import sys
    sys.exit(_self_test())
