"""数据清理（0.5.2）。

项目从 0.3 用到现在，有两处是**只增不减**的：

  ① `data/grades.db` 的 grades / snapshots 两张表（见 store.prune）
  ② `data/ec/*.pdf` —— 每份 EC 名单 4 MB 左右，一天一份

一天清一次足够了，所以不做定时器，改成**按日期判**：
把「上次清理日期」记在 `data/cleanup.json`，每次抓取完成后看一眼，
今天还没清过就清一次。

★ 为什么不用定时器：组件不是常驻进程（开机才启、随时可能关），
  定时器会丢；而「今天清过没有」这个判断永远算得出来。
"""
from __future__ import annotations

import json
import logging
import sys
import shutil
import os
import time
from datetime import date
from pathlib import Path

from . import config

log = logging.getLogger("cleanup")

STATE_FILE = config.DATA_DIR / "cleanup.json"

#  EC 名单 PDF 保留规则：最多留这么多份 / 最多留这么多天
#   （.txt 旁文件不删 —— 那是文字，很小且是扫描真正要用的东西）
KEEP_EC_PDFS = 30
KEEP_EC_DAYS = 60


def _load() -> dict:
    try:
        d = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save(d: dict) -> None:
    try:
        config.ensure_dirs()
        tmp = STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(STATE_FILE)
    except Exception as e:
        log.debug("写清理状态失败：%s", e)


def prune_ec_dir() -> int:
    """删掉太旧 / 太多的 EC 名单 PDF。返回删了几份。"""
    d = Path(config.DATA_DIR) / "ec"
    if not d.exists():
        return 0

    files: list[tuple[Path, float]] = []
    for p in d.glob("*.pdf"):
        try:
            files.append((p, p.stat().st_mtime))
        except Exception:
            continue
    if not files:
        return 0

    #  新的排在前面
    files.sort(key=lambda x: x[1], reverse=True)
    cutoff = time.time() - KEEP_EC_DAYS * 86400

    removed = 0
    for i, (p, mtime) in enumerate(files):
        too_many = i >= KEEP_EC_PDFS
        too_old = mtime < cutoff
        if not (too_many or too_old):
            continue
        try:
            p.unlink()
            removed += 1
        except Exception as e:
            log.debug("删除 %s 失败：%s", p.name, e)
    return removed


def maybe_daily(force: bool = False) -> dict:
    """今天还没清过就清一次。返回本次清理结果。"""
    today = date.today().isoformat()
    st = _load()
    if not force and st.get("last") == today:
        return {"ok": True, "skipped": True, "last": today}

    from . import store

    r = store.prune()
    n_ec = prune_ec_dir()
    #  磁盘瘦身：临时解包残留每天都清；浏览器缓存只在浏览器没运行时清
    tmp = prune_temp_extract()
    bc = browser_cache(include_service_worker=False)
    out = {"ok": True, "last": today,
           "grades": r.get("grades", 0),
           "snapshots": r.get("snapshots", 0),
           "ec_pdfs": n_ec,
           "temp_freed": tmp.get("freed", 0),
           "cache_freed": bc.get("freed", 0)}
    st["last"] = today
    st["last_result"] = out
    _save(st)
    return out


# ══════════════════════════════════════════════════════════════════
#  浏览器缓存清理（0.5.2 瘦身）
#
#  【为什么要清】
#    实测组件自己的两个浏览器目录合计 2358.8 MB，其中 1553.7 MB 是
#    浏览器自己的缓存、遥测、扩展预下载与 DRM 组件：
#
#        Service Worker 缓存   770.1 MB
#        扩展组件预下载        402.6 MB   ← 两个目录各存一份，纯重复
#        浏览器遥测            337.2 MB   ← 同上，重复
#        DRM 组件              43.8 MB
#
#    这些内容对抓取毫无用处，删掉之后 Edge 下次自己重建。
#
#  【安全边界 —— 这条必须守住】
#    登录态在下列文件里，**它们都不在删除清单中**：
#        Default/Network/Cookies      ← Teams / ManageBac 的登录 Cookie
#        Default/Login Data            ← 保存的密码
#        Local State                   ← 配置文件加密键
#        Default/Preferences           ← 站点权限与设置
#    删除清单里只有纯缓存目录。
#
#  【为什么必须先确认浏览器没在运行】
#    Chromium 在运行时被删数据目录里的文件，可能把索引写坏。
#    所以每次清理前先看该目录的调试端口有没有响应。
# ══════════════════════════════════════════════════════════════

#  删掉会自动重建、且不含任何登录态的目录
BROWSER_CACHE_SUBDIRS = (
    "ProvenanceData",           # 浏览器遥测
    "component_crx_cache",      # 扩展组件预下载
    "WidevineCdm",              # DRM 组件
    "GrShaderCache",            # 着色器缓存
    "ShaderCache",
    "GPUCache",
    "Default/Cache",
    "Default/Code Cache",
    "Default/GPUCache",
    #  Edge 自己的功能缓存（清理后按需重建，实测约 62 MB）
    "extensions_crx_cache",
    "Subresource Filter",
    "Edge Entity Extraction",
    "Edge Shopping",
    "Edge Wallet",
)

#  可选：Service Worker 缓存。Teams 的离屏资源在这里，
#  清掉之后首次加载会重新下载（慢一点），所以默认不清。
BROWSER_SW_SUBDIR = "Default/Service Worker/CacheStorage"

#  这两个目录属于本程序自己，绝不去碰用户日常使用的 Edge
BROWSER_PROFILES = ("browser-profile", "schedule-profile")


def _tree_size(p: Path) -> int:
    if not p.exists():
        return 0
    total = 0
    for root, _dirs, files in os.walk(p):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _rm_tree(p: Path) -> int:
    """删掉一个目录，返回释放的字节数。删不掉的跳过（可能被占用）。"""
    if not p.exists():
        return 0
    freed = _tree_size(p)
    try:
        shutil.rmtree(p, ignore_errors=True)
    except Exception as e:
        log.debug("删除 %s 失败：%s", p, e)
        return 0
    return 0 if p.exists() else freed


def _profile_busy(prof: Path) -> bool:
    """这个浏览器数据目录现在是不是正在被使用。"""
    try:
        from . import edge  # noqa: PLC0415
        if edge.is_edge_running(prof):
            return True
    except Exception:
        pass
    #  退一步：调试端口文件存在且进程还在（即便端口暂时不通）也算忙
    try:
        from . import cdp  # noqa: PLC0415
        if cdp.list_profile_pids(prof):
            return True
    except Exception:
        pass
    return False


def browser_cache(include_service_worker: bool = False) -> dict:
    """清掉本程序两个浏览器目录里的可重建缓存。

    返回 {"ok", "freed", "details": [{profile, freed|skipped}]}
    """
    details: list[dict] = []
    total = 0
    for name in BROWSER_PROFILES:
        prof = Path(config.DATA_DIR) / name
        if not prof.exists():
            details.append({"profile": name, "skipped": "目录不存在"})
            continue
        if _profile_busy(prof):
            details.append({"profile": name, "skipped": "浏览器正在运行"})
            continue
        subs = list(BROWSER_CACHE_SUBDIRS)
        if include_service_worker:
            subs.append(BROWSER_SW_SUBDIR)
        freed = 0
        for sub in subs:
            freed += _rm_tree(prof / sub)
        total += freed
        details.append({"profile": name, "freed": freed})
        if freed:
            log.info("浏览器缓存清理：%s 释放 %.1f MB", name, freed / 1024 / 1024)
    return {"ok": True, "freed": total, "details": details}


def prune_temp_extract(min_age_sec: float = 3600.0) -> dict:
    """清理 PyInstaller onefile 在 %TEMP% 留下的 `_MEI*` 残留目录。

    【为什么会有残留】
      打包成单文件后，每次启动都会把内容解包到 `%TEMP%/_MEIxxxx`，正常退出时
      自删。但被强制结束、崩溃、或替换安装时杀掉进程，就会留下。实测见过
      11 个残留目录、325.4 MB。

    【为什么只删「够老的」】
      同时启动的兄弟进程（例如单实例守卫刚抢锁时）也各有一个 `_MEI` 目录，
      删掉正在运行的那个会立刻崩溃。所以只删一小时以前的，并跳过本进程自己。
    """
    tmp = os.environ.get("TEMP") or os.environ.get("TMP") or ""
    if not tmp:
        return {"ok": True, "freed": 0, "removed": 0}
    mine = getattr(sys, "_MEIPASS", "") or ""
    mine_name = os.path.basename(mine) if mine else ""
    cutoff = time.time() - max(0.0, min_age_sec)
    freed = 0
    removed = 0
    try:
        entries = list(Path(tmp).glob("_MEI*"))
    except Exception:
        return {"ok": True, "freed": 0, "removed": 0}
    for p in entries:
        try:
            if not p.is_dir():
                continue
            if mine_name and p.name == mine_name:
                continue
            if p.stat().st_mtime > cutoff:
                continue
            size = _tree_size(p)
            shutil.rmtree(p, ignore_errors=True)
            if not p.exists():
                freed += size
                removed += 1
        except Exception as e:
            log.debug("清理临时解包目录失败 %s：%s", p, e)
    if removed:
        log.info("清理临时解包残留：%d 个目录，释放 %.1f MB",
                 removed, freed / 1024 / 1024)
    return {"ok": True, "freed": freed, "removed": removed}
