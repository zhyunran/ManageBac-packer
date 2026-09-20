"""配置与路径。"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# ══════════════════════════════════════════════════════════════════
#  路径解析（ 必须同时支持「源码运行」和「打包成 exe 运行」）
#
#  两种模式的差别很大，搞错了会出现「打包后打不开」：
#
#    源码运行
#      __file__ = D:\...\campus-pulse\app\config.py
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

      2. %LOCALAPPDATA%\\CampusPulse
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
        candidates.append(Path(local) / "CampusPulse")

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
    return Path(tempfile.gettempdir()) / "CampusPulse"


RES_DIR = _resource_root()
ROOT = _pick_writable_root()


def _load_credentials() -> dict:
    """读 credentials.json（没有就返回空 dict）。

     设计说明：
      凭据文件不强制存在 —— 读不到时回退到默认值，
      程序不会崩，只会在需要登录时提示你配置。

     打包版本的凭据存在 ROOT（用户目录），不在临时解压目录里，
      所以卸载重装/升级 exe 都不会丢账号。
      同时兼容读一下 RES_DIR —— 方便「把凭据一起打包」的定制场景。
    """
    for base in (ROOT, RES_DIR):
        try:
            p = base / "credentials.json"
            if p.exists():
                d = json.loads(p.read_text(encoding="utf-8"))
                if d:
                    return d
        except Exception:
            continue
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


BASE_URL = (os.environ.get("MB_BASE_URL")
    or _origin(_MB.get("url") or "")
    or _DEFAULT_BASE
)

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
RECON_FILE = DATA_DIR / "recon.json"     # 接口侦察结果
RECON_DIR = DATA_DIR / "recon"           # 侦察产物目录（summary.txt / report.json / html）

# ---------- 取数 ----------
PAGE_TIMEOUT_MS = 45_000
NAV_TIMEOUT_MS = 60_000

# ---------- 自动刷新 ----------
# 后台自动抓取间隔（秒）。
#
#  为什么是 30 分钟（而不是 30 秒）
#   站点有请求限速（429）。实测：频繁抓取（尤其并行）很容易触发，
#   触发后不但要等几分钟，还可能被当成「登录失效」引发连锁问题。
#   而作业/成绩数据本来就不会高频变化，30 分钟完全够用。
#   需要立即看最新数据时，界面右上角有手动刷新按钮。
AUTO_REFRESH_SEC = 30 * 60          # 30 分钟

# 界面上显示的刷新周期文案
AUTO_REFRESH_TEXT = "30 分钟"

# 抓取失败后的退避上限（秒），避免连续失败时反复重试
AUTO_REFRESH_MAX_BACKOFF = 900
# 两次抓取之间至少间隔（秒），防止频繁触发
AUTO_REFRESH_MIN_GAP = 20

# ---------- 自动登录（全自动，不让用户跑脚本）----------
# 登录态接近过期时，提前多久在后台自动重新登录（秒）。
# Cookie 有效期约 30 天，提前 2 天续期。
AUTO_RELOGIN_BEFORE = 2 * 86400

# 会话过期时，是否自动弹出浏览器窗口让用户手动登录。
#   True  → 自动尝试无头登录；失败就弹出有头窗口（用户只需点几下）
#   False → 只尝试无头登录
AUTO_LOGIN_ALLOW_POPUP = True

# 自动登录的最短间隔（秒）。防止失败后反复重试触发风控/锁号。
# 登录失败时**一定会退避**，这里是最小值。
AUTO_LOGIN_MIN_GAP = 10 * 60


def ensure_dirs() -> None:
    for d in (DATA_DIR, WEB_DIR, LOG_DIR, BROWSER_PROFILE_DIR, RECON_DIR):
        d.mkdir(parents=True, exist_ok=True)
