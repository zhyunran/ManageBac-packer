"""开机预热 —— 后台静默抓取，让界面「打开即有内容」。

【用户要求】
    开机一瞬间不显示任何窗口，后台把数据抓好；
    开机 1 分钟后打开界面就不必再加载。

【实现】
    在 Windows 启动文件夹放一个**隐藏窗口**的快捷方式：
        wscript.exe //B "<项目目录>/mb_warmup_launch.vbs"
    VBS 里用 `WScript.Shell.Run(cmd, 0, False)`
    —— 第二个参数 0 = SW_HIDE（隐藏窗口），彻底杜绝闪窗。

【三个关键保证】
    1. **不显示窗口** —— wscript + VBS SW_HIDE + pythonw 三层保证
    2. **不抢资源** —— 延迟若干秒再启动，避开开机高峰
    3. **只跑一次** —— 缓存 30 分钟内不重复抓

用法：
    uv run make_startup.py            查看状态
    uv run make_startup.py install    安装
    uv run make_startup.py uninstall  卸载
    uv run make_startup.py test       立刻试跑
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from app import config  # noqa: E402

LNK_NAME = "ManageBac 预热.lnk"
VBS_NAME = "mb_warmup_launch.vbs"

STARTUP_DIR = Path(os.environ.get("APPDATA", "")) / (r"Microsoft\Windows\Start Menu\Programs\Startup"
)

# 开机后延迟多少秒开始抓（避开开机高峰，太多程序同时启动会互相拖慢）
BOOT_DELAY_SEC = 20


# ---------------------------------------------------------------- 工具

def pythonw() -> Path:
    """找 pythonw.exe（无控制台窗口的 Python）。"""
    p = ROOT / ".venv" / "Scripts" / "pythonw.exe"
    if p.exists():
        return p
    p2 = ROOT / ".venv" / "Scripts" / "python.exe"
    if p2.exists():
        return p2
    return Path(sys.executable)


def make_shortcut(target: Path, lnk: Path, args: str = "",
                  workdir: Path | None = None) -> None:
    """用 PowerShell 的 WScript.Shell 创建 .lnk。"""
    wd = str(workdir or ROOT)
    ps = (f"$W = New-Object -ComObject WScript.Shell; "
        f"$s = $W.CreateShortcut('{lnk}'); "
        f"$s.TargetPath = '{target}'; "
        f"$s.Arguments = '{args}'; "
        f"$s.WorkingDirectory = '{wd}'; "
        f"$s.WindowStyle = 7; "
        f"$s.Description = 'ManageBac-packer后台预热'; "
        f"$s.Save(); "
        f"Write-Output 'created'"
    )
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
        check=True, capture_output=True, text=True,
    )


def write_vbs() -> Path:
    """生成一个隐藏窗口的启动脚本。

    为什么还要 VBS？
        pythonw.exe 本身没有控制台，但异常时 Windows 仍可能短暂闪窗。
        用 VBS 的 `WScript.Shell.Run(cmd, 0, False)` 明确指定「隐藏窗口」，
        可以彻底避免。
    """
    vbs = ROOT / VBS_NAME
    py = pythonw()
    warm = ROOT / "warmup.py"

    def q(p: Path | str) -> str:
        """VBS 字符串：路径用双引号包裹。"""
        return '"' + str(p).replace('"', '""') + '"'

    content = ("' ManageBac 后台预热启动器（由 make_startup.py 自动生成，勿手动修改）\n"
        "' 作用：开机后静默抓取数据，全程不显示任何窗口。\n"
        "Option Explicit\n"
        "Dim sh, cmd\n"
        "Set sh = CreateObject(\"WScript.Shell\")\n"
        f"WScript.Sleep {BOOT_DELAY_SEC * 1000}\n"
        f"cmd = {q(py)} & \" \" & {q(warm)} & \" --quiet\"\n"
        f"sh.CurrentDirectory = {q(ROOT)}\n"
        "' 参数2 = 0 → 隐藏窗口；参数3 = False → 不等待返回\n"
        "sh.Run cmd, 0, False\n"
    )
    vbs.write_text(content, encoding="utf-8")
    return vbs


def remove_vbs() -> bool:
    vbs = ROOT / VBS_NAME
    if vbs.exists():
        try:
            vbs.unlink()
            return True
        except Exception:
            pass
    return False


# ---------------------------------------------------------------- 命令

def install() -> int:
    config.ensure_dirs()
    STARTUP_DIR.mkdir(parents=True, exist_ok=True)

    vbs = write_vbs()
    lnk = STARTUP_DIR / LNK_NAME
    sysroot = os.environ.get("SystemRoot", r"C:\Windows")
    target = Path(sysroot) / "System32" / "wscript.exe"
    if not target.exists():
        target = Path(sysroot) / "System32" / "cscript.exe"

    print("=" * 70)
    print("  安装开机后台预热")
    print("=" * 70)
    print(f"  启动器   : {vbs.name}")
    print(f"  目标     : {target.name}  （隐藏窗口）")
    print(f"  延迟     : {BOOT_DELAY_SEC} 秒后开始（避开开机高峰）")
    print(f"  快捷方式 : {lnk}")
    print()

    try:
        make_shortcut(target, lnk, args=f'//B "{vbs}"', workdir=ROOT)
    except Exception as e:
        print(f"  [X] 创建快捷方式失败：{e}")
        print()
        print("  手动设置方法：")
        print("    1. 按 Win+R，输入 shell:startup 回车")
        print("    2. 新建快捷方式，目标填：")
        print(f'       wscript.exe //B "{vbs}"')
        return 1

    if not lnk.exists():
        print("  [!] 快捷方式未创建成功，请检查上面的输出")
        return 1

    print("  [OK] 已安装")
    print()
    print("  行为说明：")
    print(f"    * 开机后静默等待 {BOOT_DELAY_SEC} 秒")
    print("    * 后台自动登录 + 抓取（全程无窗口）")
    print("    * 抓完写入缓存 —— 之后打开小组件瞬间就有内容")
    print("    * 缓存 30 分钟内不重复抓，不浪费资源")
    return 0


def uninstall() -> int:
    print("=" * 70)
    print("  卸载开机后台预热")
    print("=" * 70)
    lnk = STARTUP_DIR / LNK_NAME
    n = 0
    if lnk.exists():
        try:
            lnk.unlink()
            print("  [OK] 已删除启动项")
            n += 1
        except Exception as e:
            print(f"  [X] 删除启动项失败：{e}")
    else:
        print("  [i] 启动项本来就不存在")

    if remove_vbs():
        print("  [OK] 已删除启动器脚本")
        n += 1

    if not n:
        print("  [i] 无需清理")
    return 0


def status() -> int:
    lnk = STARTUP_DIR / LNK_NAME
    vbs = ROOT / VBS_NAME

    print("=" * 70)
    print("  开机后台预热 —— 状态")
    print("=" * 70)
    print(f"  启动项     : {'已安装 [OK]' if lnk.exists() else '未安装'}")
    print(f"  启动器     : {'存在 [OK]' if vbs.exists() else '不存在'}")
    print(f"  Python     : {pythonw()}")

    from app import warmup

    age = warmup.cache_age()
    print()
    print("  ── 数据新鲜度 ──")
    if age < 1e8:
        mins = int(age // 60)
        print(f"  缓存年龄   : {mins} 分钟  "
              f"({'新鲜' if warmup.cache_is_fresh() else '偏旧'})")
    else:
        print("  缓存年龄   : 还没有缓存")

    st = warmup.read_status()
    print()
    print("  ── 上次预热 ──")
    if not st:
        print("  还没跑过")
    else:
        print(f"  时间   : {st.get('at', '?')}")
        print(f"  结果   : {'成功 [OK]' if st.get('ok') else '未成功'}")
        print(f"  说明   : {st.get('message', '')}")
        if st.get("tasks") is not None:
            print(f"  数据   : {st.get('tasks')} 个任务 / "
                  f"{st.get('courses')} 门课程 / 课表 {st.get('lessons')} 节")
        if st.get("skipped"):
            print(f"  （跳过抓取：{st.get('reason')}）")

    try:
        from app import httpclient

        cd = httpclient.cooldown_remaining()
        if cd > 0:
            print()
            print(f"  [!] 站点限速冷却中，还剩 {int(cd)} 秒")
    except Exception:
        pass
    return 0


def test() -> int:
    print("=" * 70)
    print("  立刻试跑一次预热（模拟开机行为）")
    print("=" * 70)
    cmd = [sys.executable, str(ROOT / "warmup.py"), "--quiet"]
    print(f"  $ {' '.join(str(c) for c in cmd)}\n")
    rc = subprocess.call(cmd, cwd=str(ROOT))
    print()
    print("  ── 结果 ──")
    status()
    return rc


def main() -> int:
    action = (sys.argv[1] if len(sys.argv) > 1 else "status").lower()
    table = {
        "install": install, "add": install, "on": install, "装": install,
        "uninstall": uninstall, "remove": uninstall, "off": uninstall,
        "卸载": uninstall,
        "test": test, "run": test, "now": test, "试跑": test,
        "status": status, "check": status, "状态": status,
    }
    fn = table.get(action)
    if fn is None:
        print(__doc__)
        return 2
    return fn()


if __name__ == "__main__":
    raise SystemExit(main())
