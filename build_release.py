"""把「打包 → 测试 → 出安装包」串成一条命令。

为什么需要它
------------
以前这套流程是手工四步，散在记忆里：

    pyinstaller build.spec → ISCC installer.iss → sandbox 安装 → check_ui / check_static

每一步都能漏，顺序也能错（比如改了更新报告却忘了重编安装包，装出来的还是旧报告）。
这个入口把它们串成一条命令，**任何一步失败就停**，并且最后给出可发布/不可发布。

用法
----
    python build_release.py            # 完整流程
    python build_release.py --dry-run  # 只打印将要执行的步骤
    python build_release.py --skip-installer   # 只重编 exe，不重编安装包

前置
----
需要一个装了 pywebview / PyInstaller 的解释器。默认按顺序找：
    1) 环境变量 MBPK_VENV_PYTHON
    2) 同目录的 .venv\\Scripts\\python.exe
    3) --python 参数指定
Inno Setup 的 ISCC.exe 默认找 %LOCALAPPDATA%\\Programs\\Inno Setup 6\\ISCC.exe。

产物
----
    dist\\ManageBac-packer.exe
    release\\ManageBac-packer-<版本>-安装包.exe
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
RELEASE = ROOT / "release"
SPEC = ROOT / "build.spec"
ISS = ROOT / "installer.iss"

FAILED: list[str] = []


def head(n: str, title: str) -> None:
    print()
    print("=" * 70)
    print(f"  [{n}] {title}")
    print("=" * 70)


def run(cmd: list[str], cwd: Path | None = None, timeout: int = 900):
    print("  $ " + " ".join(str(c) for c in cmd))
    t0 = time.time()
    r = subprocess.run(cmd, cwd=str(cwd or ROOT), capture_output=True,
                       text=True, encoding="utf-8", errors="replace",
                       timeout=timeout)
    dt = time.time() - t0
    tail = [l for l in ((r.stdout or "") + (r.stderr or "")).splitlines() if l.strip()]
    for l in tail[-6:]:
        print("      " + l[:150])
    print(f"      → 退出码 {r.returncode}，用时 {dt:.1f} 秒")
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def version() -> str:
    m = re.search(r'^\s*version\s*=\s*"([^"]+)"',
                  (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M)
    return m.group(1) if m else "unknown"


def find_python(explicit: str | None) -> str:
    if explicit:
        return explicit
    env = os.environ.get("MBPK_VENV_PYTHON")
    if env and Path(env).exists():
        return env
    for c in (ROOT / ".venv" / "Scripts" / "python.exe",
              ROOT / ".venv" / "bin" / "python"):
        if c.exists():
            return str(c)
    return ""


def find_iscc(explicit: str | None) -> str:
    if explicit:
        return explicit
    cands = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe",
        Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
        Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"),
    ]
    for c in cands:
        if c.exists():
            return str(c)
    return ""


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-installer", action="store_true")
    ap.add_argument("--skip-tests", action="store_true")
    ap.add_argument("--python", default=None, help="用于构建的解释器")
    ap.add_argument("--iscc", default=None, help="ISCC.exe 路径")
    args = ap.parse_args()

    ver = version()
    py = find_python(args.python)
    iscc = find_iscc(args.iscc)

    print("=" * 70)
    print(f"  ManageBac-packer {ver} —— 打包 + 测试")
    print("=" * 70)
    print(f"  源码根    : {ROOT}")
    print(f"  版本      : {ver}（取自 pyproject.toml）")
    print(f"  构建解释器: {py or '[未找到]'}")
    print(f"  ISCC      : {iscc or '[未找到]'}")

    if args.dry_run:
        print("\n  [dry-run] 将要执行：")
        print("    0. 清理 app/_version.py（避免读到旧版本号）")
        print(f"    1. {py or '<python>'} -m PyInstaller build.spec --noconfirm")
        print(f"    2. {py or '<python>'} check_ui.py")
        print(f"    3. {py or '<python>'} check_static.py")
        print(f"    4. {py or '<python>'} run_tests.py")
        if not args.skip_installer:
            print(f"    5. {iscc or '<ISCC>'} installer.iss")
        print("    6. 汇报产物大小与 SHA256")
        return 0

    if not py:
        print("\n  [X] 找不到可用的解释器。用 --python 指定，或设 MBPK_VENV_PYTHON。")
        return 1

    # ── 0. 清理版本残留 ──
    #  build.spec 会用 pyproject.toml 重新生成它；留着旧的会读到过期版本号
    head(0, "清理 app/_version.py（历史踩过的坑）")
    vf = ROOT / "app" / "_version.py"
    if vf.exists():
        vf.unlink()
        print("  已删除，将由 build.spec 重新生成")
    else:
        print("  本来就不存在")

    # ── 1. 打包 ──
    head(1, "PyInstaller 打包")
    code, out = run([py, "-m", "PyInstaller", str(SPEC), "--noconfirm",
                     "--log-level", "WARN", "--workpath", str(ROOT / "build"),
                     "--distpath", str(DIST)])
    if code != 0:
        FAILED.append("PyInstaller")
        print("  [X] 打包失败")
    else:
        exe = DIST / "ManageBac-packer.exe"
        if exe.exists():
            print(f"  [OK] {exe.name}  {exe.stat().st_size:,} 字节")
        else:
            FAILED.append("产物缺失")
            print("  [X] 没有产出 dist\\ManageBac-packer.exe")
    gen = vf.read_text(encoding="utf-8").strip() if vf.exists() else ""
    print(f"  生成的版本 : {gen or '[未生成]'}")
    if vf.exists() and f'"{ver}"' not in gen:
        FAILED.append("版本号不一致")
        print(f"  [X] _version.py 里的版本与 pyproject.toml ({ver}) 不一致")

    # ── 2~4. 测试 ──
    if args.skip_tests:
        print("\n  [跳过测试]")
    else:
        for n, script in ((2, "check_ui.py"), (3, "check_static.py"), (4, "run_tests.py")):
            head(n, script)
            code, _ = run([py, str(ROOT / script)])
            if code != 0:
                FAILED.append(script)
                print(f"  [X] {script} 未通过")

        #  4a. API 面守卫：前端调的后端方法必须存在，且不能调私有方法。
        #      check_static 不覆盖这一层；漏了它就会出现「按钮点了没反应」。
        head("4a", "API 面守卫（_tools/check_api_surface.py）")
        code, _ = run([py, str(ROOT / "_tools" / "check_api_surface.py"), "--strict"])
        if code != 0:
            FAILED.append("check_api_surface.py")
            print("  [X] API 面有硬错误（前端调用了不存在的后端方法）")

        #  4b. 预览页验收：先造带假数据的预览页，再用 Edge 无头跑一遍
        #      它断言的是「界面上真的渲染出了东西」（图标、徽章、换算表），
        #      这是 check_ui（只管语法）覆盖不到的一层。跑完删掉预览页。
        head("4b", "预览页行为验收（verify_preview.py）")
        preview = ROOT / "_preview.html"
        code, _ = run([py, str(ROOT / "make_preview.py")])
        if code != 0:
            FAILED.append("make_preview.py")
            print("  [X] 预览页生成失败")
        else:
            code, _ = run([py, str(ROOT / "_tools" / "verify_preview.py")])
            if code != 0:
                FAILED.append("verify_preview.py")
                print("  [X] 预览页验收未通过")
            if preview.exists():
                preview.unlink()
                print("  已清理 _preview.html")

    # ── 5. 安装包 ──
    pkg = None
    if args.skip_installer:
        print("\n  [跳过安装包]")
    else:
        head(5, "Inno Setup 编译安装包")
        if not iscc:
            FAILED.append("ISCC 未找到")
            print("  [X] 找不到 ISCC.exe，用 --iscc 指定")
        else:
            #  先确认 installer.iss 依赖的文件都在（报告在上一级目录，靠手工放）
            m = re.search(r'Source:\s*"(\.\.\\[^"]+)"', ISS.read_text(encoding="utf-8"))
            if m:
                dep = (ROOT / m.group(1)).resolve()
                print(f"  依赖报告 : {dep}")
                print(f"  存在     : {dep.exists()}")
                if not dep.exists():
                    FAILED.append("更新报告缺失")
                    print("  [X] 报告不在位；installer.iss 约定它位于源码目录的上一级")
            code, _ = run([iscc, str(ISS)], timeout=600)
            if code != 0:
                FAILED.append("ISCC")
                print("  [X] 安装包编译失败")
            else:
                pkg = RELEASE / f"ManageBac-packer-{ver}-安装包.exe"
                if pkg.exists():
                    print(f"  [OK] {pkg.name}  {pkg.stat().st_size:,} 字节")
                else:
                    FAILED.append("安装包未产出")
                    print(f"  [X] 找不到 {pkg}")

    # ── 6. 汇总 ──
    head(6, "结果")
    for label, p in (("exe", DIST / "ManageBac-packer.exe"), ("安装包", pkg)):
        if p and Path(p).exists():
            print(f"  {label:6}: {Path(p).name}")
            print(f"           {Path(p).stat().st_size:,} 字节")
            print(f"           sha256 {sha256(Path(p))}")
    print()
    if FAILED:
        print(f"  [X] 不可发布 —— 有 {len(FAILED)} 项未过：{', '.join(FAILED)}")
        return 1
    print("  [OK] 可发布 —— 打包与全部测试通过")
    print("       提醒：版本号是否该升由你决定，本脚本不会改版本号。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
