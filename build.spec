# ══════════════════════════════════════════════════════════════════
#  CampusPulse —— PyInstaller 打包配置
#
#  用法：
#      .venv\Scripts\pyinstaller.exe build.spec --noconfirm
#
#  产物：dist\CampusPulse.exe（单文件，约 30~40 MB）
#
#   设计要点（都是踩过的坑）：
#
#    1) **必须用 spec 文件**，不能用命令行参数
#       —— 命令行写不下这么多 hiddenimports 和 excludes，
#          而且参数一长就没人能维护。
#
#    2) **onefile 而不是 onedir**
#       —— 用户要的是「双击一个文件」。
#          onedir 会产生上百个文件，对普通用户不可接受。
#        （代价：每次启动要解压到临时目录，约 1~3 秒。
#          可接受 —— 主界面用缓存秒开，解压期间用户看不出差别。）
#
#    3) **必须把 web/ 目录打进去**
#       —— 界面是 web/index.html（内含全部 CSS + JS）。
#          漏了它 exe 启动会显示空白窗口。
#
#    4) **excludes 要认真写**
#       —— 不排除的话，PyInstaller 会把整个 numpy/scipy/pandas
#          （如果环境里有）都塞进来，体积能涨到 200MB+。
#
#    5) **不打包 credentials.json**
#       —— 那是用户的账号密码。让用户第一次运行时自己填（引导页）。
#          这也是「填账密即用」这个需求的完整闭环。
# ══════════════════════════════════════════════════════════════════

import sys
from pathlib import Path

# spec 文件执行时，工作目录不一定是项目根，所以显式算出来
ROOT = Path(SPECPATH).resolve()

block_cipher = None


# ── 需要一起打包的数据文件 ──
datas = [
    #  界面文件（CSS + JS 全在里面）—— 漏了就是白窗口
    (str(ROOT / "web"), "web"),
    # 说明文档（用户想了解功能时能翻到）
    (str(ROOT / "README.md"), "."),
]

# README 不存在时不要报错（打包不该因为少个说明就失败）
datas = [(src, dst) for src, dst in datas if Path(src).exists()]


# ── 隐式导入 ──
# PyInstaller 靠静态分析找 import，但下面这些它看不见：
hiddenimports = [
    # pywebview 的 Windows 后端是按字符串动态导入的
    "webview.platforms.winforms",
    "webview.platforms.edgechromium",
    "webview.platforms.mshtml",
    # clr / pythonnet（pywebview 在 Windows 上靠它调 .NET）
    "clr",
    "clr_loader",
    # 标准库里被间接用到的
    "sqlite3",
    "sqlite3.dbapi2",
    "html.parser",
    "http.cookiejar",
    "urllib.request",
    "urllib.parse",
    "xml.etree.ElementTree",
    # beautifulsoup4 的解析器
    "bs4",
    "bs4.builder",
    "bs4.builder._htmlparser",
    # 我们自己的模块（用包内相对导入，通常能自动发现，保险起见列上）
    "app",
    "app.config",
    "app.httpclient",
    "app.pipeline",
    "app.scraper",
    "app.autologin",
    "app.warmup",
    "app.digest",
    "app.prefetch",
    "app.safelink",
    "app.backup",
    "app.store",
    "app.files",
    "app.taskdetail",
    "app.manual_done",
    "app.models",
    "app.gpa",
    "app.lock",
]


# ── 排除 ──
#  这一段直接决定 exe 是 30MB 还是 200MB。
#   原则：宁多排几个，排错了顶多某个用不到的功能失效；
#         少排了会让体积失控。
#
#  踩过的坑：一开始把 distutils / setuptools / pip 也排掉了，
#    结果打包直接失败：
#      ValueError: Target module "distutils" already imported as
#                  "ExcludedModule('distutils',)"
#    原因是 PyInstaller 自己的 hook-distutils 会去 alias 这个模块，
#    你在 excludes 里排掉它，钩子就炸了。
#    → **不要排 distutils / setuptools**，它们的体积很小，
#      不值得为省几 MB 把打包搞挂。
excludes = [
    # 科学计算（体积杀手，我们完全不用）
    "numpy", "scipy", "pandas", "matplotlib", "IPython", "notebook",
    "jupyter", "nbformat", "nbconvert", "sympy", "sklearn",
    "PIL", "Pillow", "cv2", "imageio",
    # 测试与文档工具
    "pytest", "_pytest", "unittest", "doctest", "pydoc",
    "sphinx", "docutils",
    #  打包时不能把「开发期用的浏览器自动化」带进去
    #   （我们已改成纯 HTTP，这些都不需要了）
    "playwright", "selenium", "pyppeteer", "websockets",
    # 跟本程序无关的重型库
    "tkinter.test", "test", "tests",
]


a = Analysis([str(ROOT / "widget.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)


exe = EXE(pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="CampusPulse",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,              #  UPX 压缩会让部分杀毒软件误报，关掉
    upx_exclude=[],
    runtime_tmpdir=None,
    #  console=False —— 双击时不弹黑框
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    #  图标（有就用，没有就跳过）
    icon=str(ROOT / "app.ico") if (ROOT / "app.ico").exists() else None,
)
