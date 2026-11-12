"""配置与路径。"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path

log = logging.getLogger("config")

#  凭据体检结果（"没有凭据" 与 "凭据坏了" 必须能分开）。
#  形状：{"ok": bool, "note": str, "path": str}
CRED_HEALTH: dict = {"ok": True, "note": "", "path": ""}

# ══════════════════════════════════════════════════════════════════
#  路径解析（ 必须同时支持「源码运行」和「打包成 exe 运行」）
#
#  两种模式的差别很大，搞错了会出现「打包后打不开」：
#
#    源码运行
#      __file__ = D:\...\mb-widget\app\config.py
#      → 项目根目录就是它的上上级，可读可写
#
#    打包运行（PyInstaller 单文件）
#      __file__ = C:\Users\...\AppData\Local\Temp\_MEI123456\app\config.py
#      → 这是**每次启动都重新解压**的临时目录，而且程序退出就被清理！
#        往里写数据 = 数据每次重启就丢，绝对不能用来存东西
#
#  所以必须分成两个目录：
#    · RES_DIR —— 只读资源（web/index.html 等），从 _MEIPASS 读
#    · ROOT    —— 可写数据（data/ logs/ credentials.json），放到用户目录
# ══════════════════════════════════════════════════════════════════

_FROZEN = bool(getattr(sys, "frozen", False))


def _resource_root() -> Path:
    """只读资源根目录（内含 web/ 等静态文件）。"""
    if _FROZEN:
        # PyInstaller 会把 datas 解压到 sys._MEIPASS
        base = getattr(sys, "_MEIPASS", None)
        if base:
            return Path(base)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _pick_writable_root() -> Path:
    """挑一个**确定可写**的目录来放数据。

     挑选顺序（从最便携到最保险）：

      1. exe 所在目录 / 项目根目录
         —— 用户把 exe 放桌面或 U 盘时最好，数据跟着走（便携）
         —— 但放在 Program Files 时这里不可写，所以要试

      2. %LOCALAPPDATA%\\ManageBac-packer
         —— 任何情况都能写，Windows 上「每个用户独立」的标准位置

     用「真的写一个文件试试」来判断，而不是 os.access()
      —— Windows 上 os.access 对权限的判断经常不准（UAC 虚拟化、
         只读属性、ACL 等情况它都可能给出错误答案）。
    """
    candidates: list[Path] = []

    if _FROZEN:
        candidates.append(Path(sys.executable).resolve().parent)
    else:
        candidates.append(Path(__file__).resolve().parent.parent)

    local = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if local:
        candidates.append(Path(local) / "ManageBac-packer")

    for d in candidates:
        try:
            d.mkdir(parents=True, exist_ok=True)
            probe = d / ".write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return d
        except Exception:
            continue

    # 理论上到不了这里；真到了就用临时目录兜底，至少能跑起来
    import tempfile
    return Path(tempfile.gettempdir()) / "ManageBac-packer"


RES_DIR = _resource_root()
ROOT = _pick_writable_root()


# ---------- 版本号 ----------
#
#  ★ 只认**一个**来源，别在多处写死（三处不一致的坑踩过）。
#    顺序：
#      ① 打包版：`_version.py`（由 build.spec 在打包时生成，
#         内容是 `VERSION = "0.5.0"`，从 pyproject.toml 读出来）
#      ② 源码版 / 兜底：直接读项目根的 pyproject.toml
#      ③ 都读不到："unknown"
def _read_version() -> str:
    #  ① 打包时生成的
    try:
        from app import _version as _v
        v = str(getattr(_v, "VERSION", "") or "").strip()
        if v:
            return v
    except Exception:
        pass
    #  ② 项目根的 pyproject.toml
    for base in (RES_DIR, Path(__file__).resolve().parent.parent):
        try:
            p = Path(base) / "pyproject.toml"
            if p.exists():
                m = re.search(r'^\s*version\s*=\s*"([^"]+)"',
                              p.read_text(encoding="utf-8"), re.M)
                if m:
                    return m.group(1)
        except Exception:
            continue
    return "unknown"


VERSION = _read_version()


def _load_credentials() -> dict:
    """读 credentials.json（没有就返回空 dict）。

     设计说明：
      凭据文件不强制存在 —— 读不到时回退到默认值，
      程序不会崩，只会在需要登录时提示你配置。

     打包版本的凭据存在 ROOT（用户目录），不在临时解压目录里，
      所以卸载重装/升级 exe 都不会丢账号。
      同时兼容读一下 RES_DIR —— 方便「把凭据一起打包」的定制场景。

    ★ 区分「没有凭据」与「凭据坏了」：
      以前两者都返回 `{}`，于是 credentials.json 一旦被写坏（或被杀软
      加密/截断），用户会**莫名其妙回到首次引导页**，看起来像「账号丢了」
      而程序一个字都不解释。现在把坏的原因记进 CRED_HEALTH，
      由界面如实说出来（见 widget.api_health / app.js 的启动体检）。

      注意：这里**不删**坏文件 —— 里面的账号可能还能人工救回来，
      删了就真没了。
    """
    global CRED_HEALTH
    for base in (ROOT, RES_DIR):
        p = base / "credentials.json"
        if not p.exists():
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            CRED_HEALTH = {
                "ok": False,
                "note": f"凭据文件读不出来（{type(e).__name__}）：{p.name}",
                "path": str(p),
            }
            log.warning("凭据文件损坏：%s（%s）", p, e)
            continue
        if isinstance(d, dict) and d:
            CRED_HEALTH = {"ok": True, "note": "", "path": str(p)}
            return d
        #  存在但内容为空/不是对象 —— 也算异常，但不是「读不出来」
        CRED_HEALTH = {
            "ok": False,
            "note": f"凭据文件内容不完整：{p.name}",
            "path": str(p),
        }
        log.warning("凭据文件内容不完整：%s", p)
        continue
    if CRED_HEALTH.get("ok") is False:
        return {}
    CRED_HEALTH = {"ok": True, "note": "", "path": ""}
    return {}


def save_credentials(data: dict) -> Path:
    """写 credentials.json（ 首次运行引导页用）。

    原子写：先写临时文件再替换，避免写一半断电导致凭据损坏。
    """
    ensure_dirs()
    p = ROOT / "credentials.json"
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(p)
    # 尽量收紧权限（Windows 上仅作尽力而为，失败不影响功能）
    try:
        os.chmod(p, 0o600)
    except Exception:
        pass
    return p


def has_credentials() -> bool:
    """是否已经填过账号密码（用于判断要不要显示引导页）。"""
    mb = (CREDENTIALS.get("_managebac") or {})
    return bool((mb.get("login") or "").strip()
                and (mb.get("password") or "").strip())


CREDENTIALS = _load_credentials()

# ---------- 站点 ----------
# 优先级：环境变量 > credentials.json 里的 url > 默认值
#  换学校只需改 credentials.json 的 url，不用改代码
_MB = CREDENTIALS.get("_managebac") or {}
_DEFAULT_BASE = "https://your-school.managebac.cn"


def _origin(u: str) -> str:
    """从完整 URL 里取出「协议+域名」，如 https://x.managebac.cn"""
    u = (u or "").strip()
    if not u:
        return ""
    m = u.split("://", 1)
    if len(m) == 2:
        proto, rest = m
        host = rest.split("/", 1)[0]
        return f"{proto}://{host}" if host else ""
    return ""


_BASE_FROM_ENV = (os.environ.get("MB_BASE_URL") or "").strip()
_BASE_FROM_CRED = _origin(_MB.get("url") or "")

BASE_URL = _BASE_FROM_ENV or _BASE_FROM_CRED or _DEFAULT_BASE

#  ★ 当前用的地址是不是**占位符** ★
#    以前这里静默退回 your-school.managebac.cn：
#    凭据里没写 url 时，程序照样发请求，然后把 404/超时当成「网站挂了」，
#    用户完全看不出真正原因是「学校网址还没填」。
#    现在把这件事变成可判断的状态，上层据此给出明确提示。
BASE_URL_IS_PLACEHOLDER = not (_BASE_FROM_ENV or _BASE_FROM_CRED)


def has_site() -> bool:
    """是否已经有真实的学校网址（不是占位符）。"""
    return not BASE_URL_IS_PLACEHOLDER

# ---------- 路径 ----------
#  数据目录（可写）—— 打包后落在 %LOCALAPPDATA%
DATA_DIR = ROOT / "data"                 # 登录态 / 缓存 / 数据库
#  静态资源（只读）—— 打包后在临时解压目录，**不能往这里写**
WEB_DIR = RES_DIR / "web"
LOG_DIR = ROOT / "logs"
# 组件自己的浏览器数据目录（登录态存这里，与日常 Edge 完全隔离）
BROWSER_PROFILE_DIR = DATA_DIR / "browser-profile"
STATE_FILE = DATA_DIR / "state.json"     # Playwright storage_state
CACHE_FILE = DATA_DIR / "cache.json"     # 最近一次抓取结果
DB_FILE = DATA_DIR / "grades.db"         # 成绩历史库（SQLite）

# ---------- 自动刷新 ----------
# 后台自动抓取间隔（秒）。
#
#  0.5.1 起从 30 分钟改为 5 分钟（用户要求）。
#   风险：站点有请求限速（429），频繁抓取更容易撞上。
#   缓解：失败走指数退避（封顶 AUTO_REFRESH_MAX_BACKOFF）。
#   手动刷新按钮没有额外的最小间隔限制 —— 点一次抓一次，
#   连点会被 pipeline 的「正在抓取就跳过」挡住（见 pipeline.refresh_async）。
AUTO_REFRESH_SEC = 5 * 60           # 5 分钟

# 界面上显示的刷新周期文案
AUTO_REFRESH_TEXT = "5 分钟"

# 抓取失败后的退避上限（秒），避免连续失败时反复重试
AUTO_REFRESH_MAX_BACKOFF = 900

# ---------- 自动登录（全自动，不让用户跑脚本）----------
# 会话过期时，是否自动弹出浏览器窗口让用户手动登录。
#   True  → 自动尝试无头登录；失败就弹出有头窗口（用户只需点几下）
#   False → 只尝试无头登录
#
#  注：登录态「提前多久续期」不在这里配置。实际值是 autologin 里
#  jwt_expired(token, margin=3600)，即到期前 1 小时开始续期。
AUTO_LOGIN_ALLOW_POPUP = True

# 自动登录的最短间隔（秒）。防止失败后反复重试触发风控/锁号。
# 登录失败时**一定会退避**，这里是最小值。
AUTO_LOGIN_MIN_GAP = 10 * 60

# ---------- Teams 抓取（0.5.1）----------
# 增量抓取 + 历史合并总开关。
#   True  → 全量收取时：
#             ① 频道滚到「整屏都是上次抓过的消息」就停（省时大头：
#               没新消息的频道从最长 120 秒缩到几秒）；
#             ② 抓完与上次缓存合并，部分抓取 / 单频道失败不再丢历史。
#   False → 完全回到 0.5.0 行为（每频道都从头滚到尾、结果直接覆盖）。
#  上次抓取标记为 partial（到时间上限被截断）时，本次自动走全量。
TEAMS_INCREMENTAL = True

# ---------- 用哪个浏览器（0.5.2）----------
#  ★ 以前写死 Edge（只认 msedge.exe）★
#    后果很实在：用户日常用 Chrome、Teams 只在 Chrome 里登录过，
#    程序却硬要 Edge，逼他再去 Edge 登一次 —— 这不是通用程序该有的行为。
#
#  auto          自动（默认）：优先**系统默认浏览器**，再 Edge → Chrome → Brave → Chromium
#  edge/chrome/brave/chromium   指定某一款
#  C:\路径\xxx.exe             直接指定可执行文件
#  环境变量 MBPK_BROWSER 可覆盖（不用改文件）。
BROWSER = os.environ.get("MBPK_BROWSER", "auto")

#  是否复用**日常浏览器的登录态**（而不是程序自己的独立 profile）。
#  True  → 直接把系统上那个浏览器的 User Data 目录交给 CDP 用，
#          你在 Chrome 里登过 Teams，程序就能直接用同一份登录态。
#          代价：① 该浏览器必须先完全退出（否则它会把新进程转手给已开实例）；
#                ② 程序的缓存/会话会和日常浏览写在一起。
#  False → 用 data/browser-profile（独立、干净，但需要在新 profile 里登录一次）。
BROWSER_REUSE_PROFILE = (
    os.environ.get("MBPK_BROWSER_REUSE_PROFILE", "").strip().lower()
    in ("1", "true", "yes", "on")
)

#  ★ 也支持用文件配置，免得逼用户去设环境变量 ★
#    data/browser.json：
#        {"browser": "chrome", "reuse_profile": true}
#    环境变量优先级更高（临时试验用）。
try:
    _bf = (DATA_DIR / "browser.json")
    if _bf.is_file():
        import json as _json
        _bd = _json.loads(_bf.read_text(encoding="utf-8")) or {}
        if isinstance(_bd, dict):
            if not os.environ.get("MBPK_BROWSER") and _bd.get("browser"):
                BROWSER = str(_bd["browser"]).strip()
            if _bd.get("reuse_profile") is not None:
                BROWSER_REUSE_PROFILE = bool(_bd["reuse_profile"])
except Exception:
    pass


def ensure_dirs() -> None:
    for d in (DATA_DIR, WEB_DIR, LOG_DIR, BROWSER_PROFILE_DIR):
        d.mkdir(parents=True, exist_ok=True)
    #  数据目录尽量收紧权限（Windows 上登入用户自己的目录，
    #   这是「尽力而为」：失败不影响功能）。
    #   凭据单独一份 0600（见 save_credentials）。
    for d in (ROOT, DATA_DIR):
        try:
            os.chmod(d, 0o700)
        except Exception:
            pass


#  ★ 与 CampusDesk 一致的做法：数据目录不允许被当成「共享目录」使用。
#    这里不是防黑客，而是防自己 —— 例如以后加了导出功能，
#    别不小心把整个 data/ 打包给别人。
#    真正的执行点是 app/backup.py 的凭据自检（导出前后都跑），
#    这份名单供它比对。
PROTECTED_NAMES = ("credentials.json", "session_cookies.json",
                   "schedule_token.json")
