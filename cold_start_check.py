"""冷启动自查 —— 模拟「用户第一次打开」（深度适配 Windows）。

用法：
    .venv\\Scripts\\python.exe cold_start_check.py

检查项（按用户实际使用顺序）：
    1. 环境（Python / 依赖 / WebView2 / Edge）
    2. 目录与文件完整性
    3. 配置（凭据、站点地址）
    4. 数据文件可读（缓存 / 手动标记 / 晚报 / 备份）
    5. 各模块能否导入
    6. 关键函数能不能正常返回（不发网络请求）
    7. 界面文件语法
    8. 桌面快捷方式指向对不对（Windows 特有）
    9. 开机启动是否装好（Windows 特有）
   10. 上次运行留下的垃圾（锁文件 / 临时文件）
"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

PASS, WARN, FAIL = 0, 0, 0
ISSUES: list[tuple[str, str]] = []


def head(title: str) -> None:
    print(f"\n{'-' * 66}")
    print(f"  {title}")
    print(f"{'-' * 66}")


def ok(msg: str) -> None:
    global PASS
    PASS += 1
    print(f"  [OK]   {msg}")


def warn(msg: str, fix: str = "") -> None:
    global WARN
    WARN += 1
    ISSUES.append(("警告", msg))
    print(f"  [!]    {msg}")
    if fix:
        print(f"         → {fix}")


def bad(msg: str, fix: str = "") -> None:
    global FAIL
    FAIL += 1
    ISSUES.append(("错误", msg))
    print(f"  [X]    {msg}")
    if fix:
        print(f"         → {fix}")


def main() -> int:
    print("=" * 66)
    print("  冷启动自查（模拟用户第一次打开）")
    print("=" * 66)
    print(f"  项目目录：{ROOT}")
    print(f"  解释器  ：{sys.executable}")

    # ── 1. 环境 ──
    head("1. 环境")

    v = sys.version_info
    if v >= (3, 11):
        ok(f"Python {v.major}.{v.minor}.{v.micro}")
    else:
        bad(f"Python {v.major}.{v.minor} 太旧（需要 ≥ 3.11）")

    # 是不是 venv shim（会影响窗口响应 —— 已修，但报一下）
    parts = [p.lower() for p in Path(sys.executable).resolve().parts]
    if ".venv" in parts and "scripts" in parts:
        ok("解释器在 .venv 里（uv 的 shim，程序会自己处理）")
    else:
        ok("解释器是真实 Python（最理想）")

    # 依赖
    for mod, note in [
        ("webview", "pywebview"), ("bs4", "beautifulsoup4"),
        ("websocket", "websocket-client"),
    ]:
        try:
            m = importlib.import_module(mod)
            ver = getattr(m, "__version__", "?")
            ok(f"{note:18} {ver}")
        except ImportError:
            bad(f"{note} 未安装", f"uv sync")

    # Windows 特有：WebView2 运行时
    if os.name == "nt":
        import winreg
        found = False
        for hive, key in [
            (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients"
             r"\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
            (winreg.HKEY_CURRENT_USER,
             r"SOFTWARE\Microsoft\EdgeUpdate\Clients"
             r"\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
        ]:
            try:
                with winreg.OpenKey(hive, key) as k:
                    ver = winreg.QueryValueEx(k, "pv")[0]
                    ok(f"WebView2 Runtime  {ver}（界面渲染依赖它）")
                    found = True
                    break
            except OSError:
                continue
        if not found:
            bad("WebView2 Runtime 未安装（窗口跑不起来）",
                "Win11 一般自带；否则从微软官网装 Evergreen Runtime")

        # Edge（自动登录时用）
        edge = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
        if edge.exists():
            ok("Microsoft Edge 已安装（自动登录备用）")
        else:
            warn("没找到 msedge.exe",
                 "纯 HTTP 模式不需要它；自动登录才会用")

    # ── 2. 目录完整性 ──
    head("2. 目录与关键文件")

    for d in ("app", "web"):
        p = ROOT / d
        if p.is_dir():
            n = len(list(p.glob("*")))
            ok(f"{d}/  ({n} 个文件)")
        else:
            bad(f"缺少目录 {d}/")

    for f, note in [
        ("widget.py", "桌面组件入口"),
        ("web/index.html", "界面"),
        ("check_ui.py", "界面语法守卫"),
        ("pyproject.toml", "依赖声明"),
    ]:
        p = ROOT / f
        if p.exists():
            ok(f"{f:22} {p.stat().st_size:>8,} 字节  ({note})")
        else:
            bad(f"缺少 {f}（{note}）")

    # ── 3. 配置 ──
    head("3. 配置")

    cred = ROOT / "credentials.json"
    if cred.exists():
        try:
            d = json.loads(cred.read_text(encoding="utf-8"))
            mb = d.get("_managebac") or {}
            if mb.get("login") and mb.get("password"):
                ok("credentials.json 已配置 ManageBac 账号")
            else:
                warn("credentials.json 里没有 ManageBac 账号信息")
            sch = d.get("_schedule") or {}
            if sch.get("login"):
                ok("credentials.json 已配置课表账号")
            else:
                warn("没有课表账号（课表功能不可用，不影响成绩）")
        except Exception as e:
            bad(f"credentials.json 解析失败：{e}")
    else:
        bad("没有 credentials.json（无法自动登录）",
            "把 credentials.example.json 复制成 credentials.json 并填账号")

    try:
        from app import config
        ok(f"站点地址  {config.BASE_URL}")
        if config.AUTO_REFRESH_SEC == 300:
            ok(f"刷新间隔  {config.AUTO_REFRESH_TEXT}"
               f"（0.5.1 起 5 分钟；失败会指数退避防限速）")
        else:
            warn(f"刷新间隔是 {config.AUTO_REFRESH_SEC} 秒"
                 f"（{config.AUTO_REFRESH_SEC // 60} 分钟）")
    except Exception as e:
        bad(f"config 模块导入失败：{e}")

    # ── 4. 数据文件 ──
    head("4. 本机数据")

    data = ROOT / "data"
    if not data.is_dir():
        warn("data/ 不存在（首次运行会创建）")
    else:
        checks = [
            ("cache.json", "抓取缓存", True),
            ("manual_done.json", "手动完成标记", False),
            ("digests.json", "晚报历史", False),
            ("autologin.json", "自动登录状态", False),
            ("warmup.json", "预热状态", False),
        ]
        for name, note, required in checks:
            p = data / name
            if p.exists():
                ok(f"{name:22} {p.stat().st_size:>8,} 字节  ({note})")
            elif required:
                warn(f"{name} 不存在（{note}）—— 首次运行会生成")
            else:
                ok(f"{name:22} 尚未产生（{note}）")

        # 检查有没有残留的锁
        lock = data / "scrape.lock"
        if lock.exists():
            age = lock.stat().st_mtime
            import time
            mins = (time.time() - age) / 60
            if mins > 10:
                warn(f"scrape.lock 残留了 {mins:.0f} 分钟",
                     "说明上次抓取没正常结束；删掉它即可（程序也会自动忽略）")
            else:
                ok(f"scrape.lock 存在（{mins:.1f} 分钟前，可能正在抓取）")

        # 导入前备份目录
        backups = list(data.glob("_before_import_*"))
        if backups:
            ok(f"有 {len(backups)} 个导入前备份目录（可以清理）")

    # ── 5. 模块导入 ──
    head("5. 模块导入")

    mods = [
        "app.config", "app.models", "app.httpclient", "app.scraper",
        "app.pipeline", "app.files", "app.taskdetail", "app.schedule",
        "app.digest", "app.manual_done", "app.warmup", "app.autologin",
        "app.prefetch", "app.safelink", "app.backup", "app.store",
    ]
    for m in mods:
        try:
            importlib.import_module(m)
            ok(m)
        except Exception as e:
            bad(f"{m} 导入失败：{type(e).__name__}: {e}")

    # ── 6. 关键函数 ──
    head("6. 关键函数（不发网络请求）")

    try:
        from app import safelink
        ok("链接安全校验可用")
        # 抽两个用例
        r1, _ = safelink.check("https://your-school.managebac.cn/student/home")
        r2, _ = safelink.check("https://your-school.managebac.cn/logout")
        if r1 and not r2:
            ok("  规则正确（正常页面放行 / logout 拦截）")
        else:
            bad("  规则异常")
    except Exception as e:
        bad(f"safelink 异常：{e}")

    try:
        from app import digest
        st = digest.status()
        ok(f"晚报状态未读={st.get('unread')} "
           f"已有 {st.get('count')} 期")
    except Exception as e:
        bad(f"digest 异常：{e}")

    try:
        from app import backup
        st = backup.status()
        ok(f"备份状态标记 {st.get('manual_done')} 条 / "
           f"晚报 {st.get('digests')} 期")
        ok(f"  导出目录     {st.get('export_dir')}")
    except Exception as e:
        bad(f"backup 异常：{e}")

    try:
        from app import prefetch
        st = prefetch.status()
        ok(f"详情预取最多 {st.get('max_prefetch')} 个 / "
           f"间隔 {st.get('gap_sec')}s")
    except Exception as e:
        bad(f"prefetch 异常：{e}")

    try:
        from app import manual_done
        ok(f"手动标记       {manual_done.count()} 条")
    except Exception as e:
        bad(f"manual_done 异常：{e}")

    try:
        from app import store
        c = store.load_cache()
        if c:
            ok(f"缓存可读       {len(c.get('tasks') or [])} 个任务 / "
               f"{len(c.get('courses') or [])} 门课程")
        else:
            warn("缓存为空（首次运行正常）")
    except Exception as e:
        bad(f"store 异常：{e}")

    # ── 7. 界面语法 ──
    head("7. 界面文件")

    html = ROOT / "web" / "index.html"
    if html.exists():
        txt = html.read_text(encoding="utf-8")
        ok(f"index.html     {len(txt):,} 字符")
        # 粗查：标签配平
        for tag in ("<script", "</script", "<style", "</style"):
            pass
        if txt.count("<script") != txt.count("</script"):
            bad("script 标签不配平")
        else:
            ok("script 标签配平")
        # emoji 检查 ——  只算「会显示给用户」的，注释里的不算
        import re
        # 先剥掉注释
        body = re.sub(r"/\*.*?\*/", "", txt, flags=re.S)     # /* ... */
        body = re.sub(r"^\s*//.*$", "", body, flags=re.M)     # // ...
        body = re.sub(r"<!--.*?-->", "", body, flags=re.S)    # <!-- -->
        emoji = re.findall(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", body)
        # 过滤掉「装饰性符号」—— 这些是刻意的排版记号，不是 emoji
        emoji = [e for e in emoji if e not in "·—–"]
        if emoji:
            warn(f"界面代码里还有 {len(emoji)} 个 emoji："
                 f"{''.join(emoji[:12])}")
        else:
            ok("界面无 emoji（已全部换成 SVG 图标）")

    # ── 8. 桌面快捷方式（Windows 特有）──
    head("8. 桌面快捷方式")

    if os.name == "nt":
        import win32com.client  # type: ignore
        try:
            shell = win32com.client.Dispatch("WScript.Shell")
            found = False
            for base in [
                Path(os.environ.get("USERPROFILE", "")) / "Desktop",
                Path(os.environ.get("USERPROFILE", "")) / "OneDrive" / "Desktop",
                Path(os.environ.get("PUBLIC", "")) / "Desktop",
            ]:
                if not base.exists():
                    continue
                for lnk in base.glob("*.lnk"):
                    try:
                        sc = shell.CreateShortCut(str(lnk))
                        if "widget.py" in (sc.Arguments or ""):
                            ok(f"{lnk.name}")
                            ok(f"  目标  {sc.TargetPath}")
                            ok(f"  参数  {sc.Arguments}")
                            found = True
                    except Exception:
                        continue
            if not found:
                warn("桌面没有小组件快捷方式",
                     "可以手动建一个，或者直接双击 widget.py 所在目录")
        except ImportError:
            # 没有 pywin32 —— 用 PowerShell 代替
            try:
                ps = subprocess.run(["powershell", "-NoProfile", "-Command",
                     "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
                     "$s=New-Object -ComObject WScript.Shell;"
                     "Get-ChildItem \"$env:USERPROFILE\\Desktop\\*.lnk\" "
                     "-EA SilentlyContinue | ForEach-Object {"
                     "$l=$s.CreateShortcut($_.FullName);"
                     "if($l.Arguments -like '*widget.py*')"
                     "{ \"$($_.Name)|$($l.TargetPath)|$($l.Arguments)\" }}"],
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=30,
                )
                out = (ps.stdout or "").strip()
                if out:
                    for line in out.splitlines():
                        parts = line.split("|")
                        ok(f"{parts[0]}")
                        if len(parts) > 1:
                            ok(f"  目标  {parts[1]}")
                else:
                    warn("桌面没有小组件快捷方式")
            except Exception as e:
                warn(f"检查快捷方式失败：{e}")

    # ── 9. 开机启动（Windows 特有）──
    head("9. 开机启动")

    if os.name == "nt":
        startup = (Path(os.environ.get("APPDATA", ""))
                   / "Microsoft/Windows/Start Menu/Programs/Startup")
        if startup.is_dir():
            lnks = [x for x in startup.glob("*")
                    if "ManageBac" in x.name or "mb_" in x.name]
            if lnks:
                for x in lnks:
                    ok(f"{x.name}")
            else:
                ok("未安装开机启动（可选功能）")
        else:
            warn("找不到启动文件夹")

    # ── 10. 垃圾清理 ──
    head("10. 运行残留")

    junk = []
    for pat, note in [
        ("logs/*.tmp", "临时日志"),
        ("data/*.tmp", "临时数据"),
        ("data/*.json.tmp", "未完成的写入"),
        ("web/*.bak_*", "界面备份"),
        ("_preview.html", "预览页（开发用）"),
    ]:
        for p in ROOT.glob(pat):
            junk.append((p, note))

    if junk:
        for p, note in junk[:8]:
            warn(f"{p.relative_to(ROOT)}  ({note})")
        if len(junk) > 8:
            print(f"         ... 还有 {len(junk) - 8} 个")
        print(f"         提示：这些都不影响运行，想清理可以手动删")
    else:
        ok("没有残留垃圾")

    # ── 汇总 ──
    print(f"\n{'=' * 66}")
    print(f"  结果：{PASS} 项通过 · {WARN} 项警告 · {FAIL} 项错误")
    print(f"{'=' * 66}")
    if ISSUES:
        print("\n  需要注意：")
        for kind, msg in ISSUES:
            print(f"    [{kind}] {msg}")
    print()
    if FAIL:
        print("  [X] 有错误，先修掉再启动")
    elif WARN:
        print("  [!] 可用，但有警告项（多数是首次运行正常现象）")
    else:
        print("  [OK] 全部通过，可以直接启动")
    print()
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
