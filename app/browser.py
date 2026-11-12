"""浏览器定位 —— 不再写死 Edge。

【为什么要有这个模块】
  原来 cdp.py 和 edge.py 各写死一份 `_EDGE_CANDIDATES`，只认 msedge.exe。
  后果：
    · 用户日常用 Chrome，Teams 只在 Chrome 里登录过 —— 程序却硬要 Edge，
      逼他再去 Edge 里登一次。
    · 机器上装了 Chrome / Brave / Chromium 但没 Edge 的，功能直接不可用。

  现在统一成「任何 Chromium 内核浏览器都能用」：
    Edge / Chrome / Brave / Chromium（可扩展），
    并且优先挑**系统默认浏览器** —— 那通常就是用户已经登录好的那个。

【怎么指定】
    config.BROWSER（或环境变量 MBPK_BROWSER）：
        "auto"          自动（默认）：默认浏览器 → Edge → Chrome → …
        "edge"/"chrome"/"brave"/"chromium"
        绝对路径       直接用它

【注意】
    程序仍然用**自己的 profile 目录**跑浏览器（data/browser-profile），
    所以第一次仍要在那个 profile 里登录一次；
    想复用日常浏览器的登录态，见 config.BROWSER_REUSE_PROFILE。
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger(__name__)


#  (key, 显示名, 额外固定路径, PATH 里的命令名)
_BROWSERS: list[tuple[str, str, list[str], str]] = [
    ("edge", "Edge", [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\EdgeCore\msedge.exe",
    ], "msedge"),
    ("chrome", "Chrome", [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
    ], "chrome"),
    ("brave", "Brave", [
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe",
    ], "brave"),
    ("chromium", "Chromium", [
        r"C:\Program Files\Chromium\Application\chrome.exe",
        r"%LOCALAPPDATA%\Chromium\Application\chrome.exe",
    ], "chromium"),
]

#  系统默认浏览器的 ProgId → 我们的 key
_PROGID_MAP = {
    "msedgehtm": "edge",
    "microsoftedge": "edge",
    "chromehtml": "chrome",
    "bravehtml": "brave",
    "chromiumhtm": "chromium",
}


def _expand(p: str) -> Path:
    return Path(os.path.expandvars(p))


def _registry_paths(exe_name: str) -> list[Path]:
    """从 App Paths 注册表读权威安装位置（应对非 C 盘 / 用户级安装）。"""
    out: list[Path] = []
    try:
        import winreg
        for root, sub in (
            (winreg.HKEY_LOCAL_MACHINE,
             rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}"),
            (winreg.HKEY_CURRENT_USER,
             rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}"),
            (winreg.HKEY_LOCAL_MACHINE,
             rf"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion"
             rf"\App Paths\{exe_name}"),
        ):
            try:
                with winreg.OpenKey(root, sub) as k:
                    val, _ = winreg.QueryValueEx(k, None)
                    if val:
                        out.append(Path(str(val).strip('"')))
            except OSError:
                continue
    except Exception:
        pass
    return out


def _candidates_for(key: str) -> list[Path]:
    for k, _name, fixed, exe in _BROWSERS:
        if k != key:
            continue
        out = [_expand(p) for p in fixed]
        for base in (os.environ.get("LOCALAPPDATA"),
                     os.environ.get("PROGRAMFILES"),
                     os.environ.get("PROGRAMFILES(X86)"),
                     os.environ.get("ProgramW6432")):
            if not base:
                continue
            if key == "edge":
                out.append(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
            elif key == "chrome":
                out.append(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")
            elif key == "brave":
                out.append(Path(base) / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe")
            elif key == "chromium":
                out.append(Path(base) / "Chromium" / "Application" / "chrome.exe")
        out.extend(_registry_paths(exe))
        found = shutil.which(exe)
        if found:
            out.append(Path(found))
        return out
    return []


def installed() -> list[tuple[str, str, Path]]:
    """本机已安装的 Chromium 内核浏览器 [(key, 显示名, 路径)]。"""
    out: list[tuple[str, str, Path]] = []
    seen: set[str] = set()
    for key, name, _fixed, _exe in _BROWSERS:
        for p in _candidates_for(key):
            try:
                if p.is_file():
                    rp = str(p.resolve()).lower()
                    if rp in seen:
                        break
                    seen.add(rp)
                    out.append((key, name, p))
                    break
            except OSError:
                continue
    return out


def default_browser_key() -> str:
    """系统默认浏览器对应的 key（不是 Chromium 内核就返回空串）。"""
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\Shell\Associations"
                r"\UrlAssociations\https\UserChoice") as k:
            prog = str(winreg.QueryValueEx(k, "ProgId")[0] or "").lower()
        for frag, key in _PROGID_MAP.items():
            if frag in prog:
                return key
    except Exception:
        pass
    return ""


def display_name(path: Path | None) -> str:
    if path is None:
        return "（未找到浏览器）"
    low = str(path).lower()
    for key, name, _f, _e in _BROWSERS:
        if key == "edge" and "msedge" in low:
            return name
        if key == "chrome" and "chrome.exe" in low and "chromium" not in low:
            return name
        if key == "brave" and "brave" in low:
            return name
        if key == "chromium" and "chromium" in low:
            return name
    return path.name


def find(force: str | None = None) -> Path | None:
    """定位一个可用的 Chromium 浏览器。

    force: 指定 key（"edge"/"chrome"/…）或绝对路径；None/""/"auto" = 自动。
    自动顺序：系统默认浏览器 → Edge → Chrome → Brave → Chromium。
    """
    if force is None:
        from . import config as _cfg
        force = getattr(_cfg, "BROWSER", "auto") or "auto"
    want = force
    want = str(want).strip()

    if want and want.lower() not in ("auto", "default"):
        #  绝对路径
        if os.path.sep in want or want.endswith(".exe"):
            p = _expand(want)
            if p.is_file():
                return p
            log.warning("指定的浏览器不存在：%s", want)
            return None
        #  key
        for p in _candidates_for(want.lower()):
            try:
                if p.is_file():
                    log.info("使用指定浏览器：%s（%s）",
                             display_name(p), p)
                    return p
            except OSError:
                continue
        log.warning("找不到浏览器「%s」，改为自动选择", want)

    avail = installed()
    if not avail:
        return None

    by_key = {k: p for k, _n, p in avail}
    dflt = default_browser_key()
    order = ([dflt] if dflt in by_key else []) + \
            [k for k in ("edge", "chrome", "brave", "chromium") if k in by_key]
    for k in order:
        if k in by_key:
            p = by_key[k]
            log.info("浏览器选型：%s（系统默认=%s）", display_name(p), dflt or "未知")
            return p
    return avail[0][2]
