"""瘦身清理的回归测试。

为什么这条测试最重要
--------------------
清理的对象是浏览器数据目录。**删错一个文件，登录态就没了** ——
用户会突然被要求重新登录 Teams / ManageBac，而且不知道该怪谁。

所以本测试的核心不是「删掉了多少」，而是：
    删除清单里**没有一个登录态文件**，
    并且浏览器正在运行时**一个文件都不删**。

覆盖：
  ① browser_cache 只删缓存目录，Cookies / Login Data / Local State /
    Preferences 等登录态文件必须原封不动
  ② 浏览器正在运行时整体跳过（Chromium 运行时删数据目录会写坏索引）
  ③ Service Worker 缓存默认不清，显式开启才清
  ④ prune_temp_extract 跳过「比阈值新的」和「本进程自己的」目录
  ⑤ maybe_daily 把两项释放量记进结果

全程在临时目录里跑，不碰真实安装。
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PASS = 0
FAIL = 0


def chk(ok: bool, label: str, extra=None) -> None:
    global PASS, FAIL
    print(f"  {'[OK]' if ok else '[X] '} {label}" + ("" if ok else f"   → {extra!r}"))
    if ok:
        PASS += 1
    else:
        FAIL += 1


def write(path: pathlib.Path, size: int = 512) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def main() -> int:
    print("=" * 76)
    print("  瘦身清理：只删缓存，绝不碰登录态")
    print("=" * 76)

    from app import cleanup, config

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="mbpk_slim_"))
    real_data = config.DATA_DIR
    real_temp = os.environ.get("TEMP")
    real_busy = cleanup._profile_busy

    try:
        config.DATA_DIR = tmp
        os.environ["TEMP"] = str(tmp / "_temp")

        # ── 准备一个像样的浏览器目录 ──
        prof = tmp / "browser-profile"
        prof2 = tmp / "schedule-profile"

        #   缓存（应被删）
        cache_files = [
            prof / "ProvenanceData" / "a.bin",
            prof / "component_crx_cache" / "b.bin",
            prof / "WidevineCdm" / "_platform_specific" / "c.bin",
            prof / "Default" / "Cache" / "d.bin",
            prof / "GrShaderCache" / "e.bin",
            prof2 / "ProvenanceData" / "a2.bin",
            prof2 / "component_crx_cache" / "b2.bin",
        ]
        for f in cache_files:
            write(f, 4096)

        #   登录态（绝不能被删）
        login_files = [
            prof / "Default" / "Network" / "Cookies",
            prof / "Default" / "Network" / "Cookies-journal",
            prof / "Default" / "Login Data",
            prof / "Default" / "Web Data",
            prof / "Local State",
            prof / "Default" / "Preferences",
            prof2 / "Default" / "Network" / "Cookies",
            prof2 / "Local State",
        ]
        for f in login_files:
            write(f, 2048)

        #   Service Worker 缓存（默认不清）
        sw = prof / "Default" / "Service Worker" / "CacheStorage" / "x.bin"
        write(sw, 8192)

        busy_before = sum(f.stat().st_size for f in prof.rglob("*") if f.is_file())

        # ── ① 正常清理 ──
        print("\n── ① 浏览器没在运行时：删缓存、留登录态 ──")
        cleanup._profile_busy = lambda p: False
        r = cleanup.browser_cache(include_service_worker=False)
        chk(r.get("ok") is True, "返回 ok", r.get("ok"))
        chk(r.get("freed", 0) > 0, f"释放了 {r.get('freed', 0)} 字节", r.get("freed"))

        gone = [f for f in cache_files if not f.exists()]
        chk(len(gone) == len(cache_files),
            f"缓存目录全部清掉（{len(gone)}/{len(cache_files)}）",
            [str(x.relative_to(tmp)) for x in cache_files if x.exists()])

        kept = [f for f in login_files if f.exists()]
        chk(len(kept) == len(login_files),
            f"登录态文件全部保留（{len(kept)}/{len(login_files)}）",
            [str(x.relative_to(tmp)) for x in login_files if not x.exists()])
        chk((prof / "Default" / "Network" / "Cookies").exists(),
            "Cookies 还在（Teams / ManageBac 的登录凭据）")
        chk((prof / "Local State").exists(), "Local State 还在（配置加密键）")

        chk(sw.exists(), "Service Worker 缓存默认不清（Teams 资源下次不用重下）")

        # ── ② 浏览器在跑：整体跳过 ──
        print("\n── ② 浏览器正在运行时：一个文件都不删 ──")
        write(prof / "ProvenanceData" / "again.bin", 4096)
        cleanup._profile_busy = lambda p: True
        r2 = cleanup.browser_cache()
        chk((prof / "ProvenanceData" / "again.bin").exists(),
            "缓存仍在（浏览器在跑，不能删）")
        details = {d.get("profile"): d for d in (r2.get("details") or [])}
        chk(any("运行" in str(d.get("skipped", "")) for d in details.values()),
            "结果里如实说明跳过原因", list(details.values())[:2])

        # ── ③ 显式开启才清 Service Worker ──
        print("\n── ③ Service Worker 缓存：显式开启才清 ──")
        cleanup._profile_busy = lambda p: False
        r3 = cleanup.browser_cache(include_service_worker=True)
        chk(not sw.exists(), "开启后 Service Worker 缓存被清掉", sw.exists())
        chk((prof / "Default" / "Network" / "Cookies").exists(),
            "顺手再确认一次：Cookies 依然在")
        chk(r3.get("freed", 0) > 0, "这次也有释放量", r3.get("freed"))

        # ── ④ 临时解包残留 ──
        print("\n── ④ _MEI 残留：只删够老的、且不是本进程的 ──")
        tdir = pathlib.Path(os.environ["TEMP"])
        tdir.mkdir(parents=True, exist_ok=True)
        old_dir = tdir / "_MEI0000old"
        new_dir = tdir / "_MEI0000new"
        mine_dir = tdir / "_MEI0000mine"
        for d in (old_dir, new_dir, mine_dir):
            write(d / "python312.dll", 2048)
        old_time = time.time() - 7200
        os.utime(old_dir, (old_time, old_time))
        #  new_dir / mine_dir 都是「刚刚」创建的

        real_meipass = getattr(sys, "_MEIPASS", "")
        sys._MEIPASS = str(mine_dir)
        try:
            r4 = cleanup.prune_temp_extract(min_age_sec=3600)
        finally:
            if real_meipass:
                sys._MEIPASS = real_meipass
            else:
                delattr(sys, "_MEIPASS")

        chk(not old_dir.exists(), "一小时前的残留被删掉", old_dir.exists())
        chk(new_dir.exists(), "刚创建的不删（可能是同时在启动的兄弟进程）",
            new_dir.exists())
        chk(mine_dir.exists(), "本进程自己的目录不删", mine_dir.exists())
        chk(r4.get("removed") == 1, f"只删了 1 个（{r4.get('removed')}）",
            r4.get("removed"))

        # ── ⑤ 每日清理汇总 ──
        print("\n── ⑤ 每日清理会把两项都记进结果 ──")
        write(prof / "ProvenanceData" / "daily.bin", 4096)
        cleanup._profile_busy = lambda p: False
        r5 = cleanup.maybe_daily(force=True)
        chk("temp_freed" in r5, "结果里有临时解包释放量", list(r5.keys()))
        chk("cache_freed" in r5, "结果里有浏览器缓存释放量", list(r5.keys()))
        chk(r5.get("cache_freed", 0) > 0, f"cache_freed={r5.get('cache_freed')}")

    finally:
        cleanup._profile_busy = real_busy
        config.DATA_DIR = real_data
        if real_temp is None:
            os.environ.pop("TEMP", None)
        else:
            os.environ["TEMP"] = real_temp
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("=" * 76)
    print(f"  {PASS} 通过 / {FAIL} 失败")
    print("=" * 76)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
