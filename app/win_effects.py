"""Windows 原生窗体效果 —— 毛玻璃 / 圆角 / 置顶 / 自绘标题栏的地基。

【为什么要单独一个模块】
    这些效果都得直接调 Windows 的 API（DWM、User32），
    调错了窗口会变成一块黑方块或干脆不显示。
    集中放一处，方便逐项开关、逐项回退。

【实测确认的能力（读 pywebview 源码 + 真机验证）】
    pywebview 的 winforms 后端本身就是 .NET Form，
    窗口创建完可以拿到 ``window.native``（BrowserForm 实例），
    再取 ``.Handle`` 就是 HWND。
    它内部已经在用这几个：
        DwmExtendFrameIntoClientArea   玻璃效果的前提
        DwmSetWindowAttribute(hwnd, 2, 2)   非客户区渲染策略
        DwmSetWindowAttribute(hwnd, 20, x)  深色/浅色标题栏
        DwmSetWindowAttribute(hwnd, 33, x)  圆角偏好  ← 圆角就靠它
    macOS 那边有个 ``vibrancy`` 参数，Windows 上没有对应实现，
    所以得自己写 —— 这个模块就是干这个的。

【两种毛玻璃，按系统版本选】
    Win11 (build 22000+)
        用 ``DWMWA_SYSTEMBACKDROP_TYPE``，值 3 = Acrylic、2 = Mica。
        系统自己画，跟着主题走，最省事也最好看。

    Win10 1803 ~ 19044
        用未公开的 ``SetWindowCompositionAttribute``，
        accent state 4 = Acrylic、3 = Blur。
        未公开 API，可能被系统更新改掉，所以要对异常全兜住。

    都失败时
        不折腾，退回不透明背景。界面仍然是圆角卡片，只是背后不透。

【一个必须记住的前提】
    毛玻璃要「透出后面的东西」，窗口自己就得是透明的。
    所以 ``transparent=True`` 是前提 —— 窗口背景设成完全透明，
    再由 DWM 在窗口背后补一层模糊。
    如果窗口是不透明的，DWM 再怎么设也看不到效果。
"""
from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes

log = logging.getLogger("win_effects")

IS_WIN = sys.platform == "win32"

# ══════════════════════════════════════════════════════════
#  DWM 属性编号
#
#  这些数字在 Windows SDK 里都有名字，这里写数字是为了
#  不依赖 pywin32（少一个依赖，打包小一点）。
# ══════════════════════════════════════════════════════════

DWMWA_NCRENDERING_POLICY = 2
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_BORDER_COLOR = 34
DWMWA_CAPTION_COLOR = 35
DWMWA_SYSTEMBACKDROP_TYPE = 38

#  圆角偏好取值
CORNER_DEFAULT = 0
CORNER_DONOTROUND = 1
CORNER_ROUND = 2
CORNER_ROUNDSMALL = 3

#  系统背景材质取值（Win11 22H2+）
BACKDROP_AUTO = 0
BACKDROP_NONE = 1
BACKDROP_MICA = 2
BACKDROP_ACRYLIC = 3
BACKDROP_TABBED = 4

#  SetWindowCompositionAttribute 的 accent state
ACCENT_DISABLED = 0
ACCENT_ENABLE_BLURBEHIND = 3
ACCENT_ENABLE_ACRYLICBLURBEHIND = 4

#  SetWindowPos
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
#  ★ 0.5.1：这个常量以前漏了定义，_set_window_pos 一调就
#    NameError，整个 SetWindowPos 通路从来没真正成功过
#   （贴角摆放、自绘拖动全受影响）。
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010

#  SetWindowRgn
WINDING = 1


# ══════════════════════════════════════════════════════════
#  句柄与系统版本
# ══════════════════════════════════════════════════════════

def hwnd_of(window) -> int:
    """从 pywebview 的 Window 对象取 HWND。

    pywebview 在建窗时把后端窗体挂在 ``window.native`` 上，
    那是个 .NET Form，``.Handle`` 才是真正的 HWND。
    """
    if not IS_WIN:
        return 0
    native = getattr(window, "native", None)
    if native is None:
        return 0
    h = getattr(native, "Handle", None)
    if h is None:
        return 0
    try:
        return int(h.ToInt32())
    except Exception:
        try:
            return int(h)
        except Exception:
            return 0


_WIN11: bool | None = None


def is_win11() -> bool:
    """是不是 Windows 11（build >= 22000）。

    决定用哪种毛玻璃方案。结果缓存 —— 这东西不会变。
    """
    global _WIN11
    if _WIN11 is not None:
        return _WIN11
    if not IS_WIN:
        _WIN11 = False
        return False
    try:
        v = sys.getwindowsversion()
        _WIN11 = v.build >= 22000
    except Exception:
        _WIN11 = False
    return _WIN11


# ══════════════════════════════════════════════════════════
#  DWM
# ══════════════════════════════════════════════════════════

def _dwm_set(hwnd: int, attr: int, value: int, size: int = 4) -> bool:
    """调 DwmSetWindowAttribute。失败返回 False，不抛异常。

    失败很常见（老系统没有新属性），所以调用方靠返回值判断，
    不要用 try 包着 —— 那样会把「不支持」和「真的出错」混在一起。
    """
    if not IS_WIN or not hwnd:
        return False
    try:
        fn = ctypes.windll.dwmapi.DwmSetWindowAttribute
        fn.argtypes = [wintypes.HWND, wintypes.DWORD,
                       ctypes.c_void_p, wintypes.DWORD]
        fn.restype = ctypes.c_long
        rc = fn(wintypes.HWND(hwnd), wintypes.DWORD(attr),
                ctypes.byref(ctypes.c_int(value)), wintypes.DWORD(size))
        return rc == 0
    except Exception as e:
        log.debug("DwmSetWindowAttribute(%s, %s) 失败：%s", attr, value, e)
        return False


def _extend_frame(hwnd: int, margin: int = 1) -> bool:
    """把 DWM 边框延伸到客户区 —— 玻璃效果的前提。

    pywebview 在 ``shadow=True`` 时自己会调一次，
    但透明窗口那条分支会跳过，所以要能自己补。
    """
    if not IS_WIN or not hwnd:
        return False

    class MARGINS(ctypes.Structure):
        _fields_ = [("cxLeftWidth", ctypes.c_int),
                    ("cxRightWidth", ctypes.c_int),
                    ("cyTopHeight", ctypes.c_int),
                    ("cyBottomHeight", ctypes.c_int)]

    try:
        fn = ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea
        fn.argtypes = [wintypes.HWND, ctypes.c_void_p]
        m = MARGINS(margin, margin, margin, margin)
        return fn(wintypes.HWND(hwnd), ctypes.byref(m)) == 0
    except Exception as e:
        log.debug("DwmExtendFrameIntoClientArea 失败：%s", e)
        return False


def set_rounded(hwnd: int, radius: str = "round") -> bool:
    """圆角窗口。

    Win11 直接告诉系统要圆角，系统自己处理抗锯齿与阴影，
    效果最正。Win10 没这个属性，只能自己裁一块圆角区域。

    radius: "round"（大圆角）/ "small"（小圆角）/ "none"（不要圆角）
    """
    if not IS_WIN or not hwnd:
        return False

    if is_win11():
        val = {"round": CORNER_ROUND,
               "small": CORNER_ROUNDSMALL,
               "none": CORNER_DONOTROUND}.get(radius, CORNER_ROUND)
        return _dwm_set(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, val)

    # ── Win10：自己裁 ──
    #  注意 SetWindowRgn 之后系统不再画阴影（区域外都是无效的），
    #  外观会朴素一些，但至少是圆角。
    if radius == "none":
        return _clear_region(hwnd)
    try:
        r = 8 if radius == "small" else 14
        w, h = _client_size(hwnd)
        if w <= 0 or h <= 0:
            return False
        rgn = ctypes.windll.gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, r, r)
        if not rgn:
            return False
        #  第三个参数 redraw=1，让系统立刻重画
        ok = bool(ctypes.windll.user32.SetWindowRgn(
            wintypes.HWND(hwnd), wintypes.HRGN(rgn), 1))
        return ok
    except Exception as e:
        log.debug("Win10 圆角裁剪失败：%s", e)
        return False


def _clear_region(hwnd: int) -> bool:
    try:
        return bool(ctypes.windll.user32.SetWindowRgn(
            wintypes.HWND(hwnd), wintypes.HRGN(0), 1))
    except Exception:
        return False


def _client_size(hwnd: int) -> tuple[int, int]:
    try:
        rect = wintypes.RECT()
        ctypes.windll.user32.GetClientRect(wintypes.HWND(hwnd),
                                           ctypes.byref(rect))
        return rect.right - rect.left, rect.bottom - rect.top
    except Exception:
        return 0, 0


def _set_accent_blur(hwnd: int, accent: int, tint: int = 0x00000000) -> bool:
    """Win10 的毛玻璃 —— 走未公开的 SetWindowCompositionAttribute。

    这是第三方工具常用的手法（TranslucentTB 那类）。
    因为是未公开 API，结构体布局必须一字不差，否则可能崩。
    所以整个包在 try 里，失败就退回不透明。
    """
    if not IS_WIN or not hwnd:
        return False

    class ACCENTPOLICY(ctypes.Structure):
        _fields_ = [("AccentState", ctypes.c_int),
                    ("AccentFlags", ctypes.c_int),
                    ("GradientColor", ctypes.c_uint),
                    ("AnimationId", ctypes.c_int)]

    class WINCOMPATTRDATA(ctypes.Structure):
        _fields_ = [("Attribute", ctypes.c_int),
                    ("Data", ctypes.POINTER(ACCENTPOLICY)),
                    ("SizeOfData", ctypes.c_size_t)]

    try:
        policy = ACCENTPOLICY()
        policy.AccentState = accent
        #  AccentFlags = 2 是「给整个窗口着色」的常见取值
        policy.AccentFlags = 2 if accent != ACCENT_DISABLED else 0
        policy.GradientColor = tint
        policy.AnimationId = 0

        data = WINCOMPATTRDATA()
        data.Attribute = 19          # WCA_ACCENT_POLICY
        data.Data = ctypes.pointer(policy)
        data.SizeOfData = ctypes.sizeof(policy)

        fn = ctypes.windll.user32.SetWindowCompositionAttribute
        fn.argtypes = [wintypes.HWND, ctypes.POINTER(WINCOMPATTRDATA)]
        return bool(fn(wintypes.HWND(hwnd), ctypes.byref(data)))
    except Exception as e:
        log.debug("SetWindowCompositionAttribute 失败：%s", e)
        return False


def _apply_win11_backdrop(hwnd: int, kind: str) -> bool:
    """Win11 的方式：告诉系统要哪种材质，系统自己画。"""
    if kind == "none":
        #  先关材质，再把边框收回去
        _dwm_set(hwnd, DWMWA_SYSTEMBACKDROP_TYPE, BACKDROP_NONE)
        return True

    val = {"acrylic": BACKDROP_ACRYLIC,
           "mica": BACKDROP_MICA,
           "tabbed": BACKDROP_TABBED}.get(kind, BACKDROP_ACRYLIC)

    #  顺序有讲究：先让 DWM 接管非客户区渲染，再设材质
    _dwm_set(hwnd, DWMWA_NCRENDERING_POLICY, 2)
    _extend_frame(hwnd, 1)
    return _dwm_set(hwnd, DWMWA_SYSTEMBACKDROP_TYPE, val)


def apply_glass(window, kind: str = "acrylic",
                tint: int = 0x00000000) -> dict:
    """给窗口上毛玻璃。返回 ``{ok, how, why?}`` 方便排查。

    kind: "acrylic" / "mica" / "none"

    调用时机很重要 —— 要在窗口**显示之后**调，
    句柄那时才稳定。``shown`` 事件里调最稳。
    """
    if not IS_WIN:
        return {"ok": False, "why": "只有 Windows 有这个效果"}

    hwnd = hwnd_of(window)
    if not hwnd:
        return {"ok": False, "why": "拿不到窗口句柄（窗口可能还没显示）"}

    if kind == "none":
        if is_win11():
            _apply_win11_backdrop(hwnd, "none")
        else:
            _set_accent_blur(hwnd, ACCENT_DISABLED)
        return {"ok": True, "how": "closed"}

    if is_win11():
        if _apply_win11_backdrop(hwnd, kind):
            return {"ok": True, "how": f"win11-{kind}"}
        #  Win11 失败也要试老办法（有些精简版系统去掉了新属性）
        log.debug("Win11 材质设置失败，改试 Win10 方案")

    accent = (ACCENT_ENABLE_ACRYLICBLURBEHIND if kind == "acrylic"
              else ACCENT_ENABLE_BLURBEHIND)
    if _set_accent_blur(hwnd, accent, tint):
        return {"ok": True, "how": f"accent-{kind}"}

    return {"ok": False, "why": "系统不支持毛玻璃（两套方案都失败）"}


# ══════════════════════════════════════════════════════════
#  置顶
# ══════════════════════════════════════════════════════════

def set_always_on_top(window, on: bool = True) -> bool:
    """把窗口钉在其他应用上面。

    pywebview 的 ``window.on_top`` 属性在部分后端上不生效，
    这里直接调 SetWindowPos，最可靠。
    """
    hwnd = hwnd_of(window)
    if not hwnd:
        return False
    try:
        fn = ctypes.windll.user32.SetWindowPos
        fn.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                       ctypes.c_int, ctypes.c_int, ctypes.c_int,
                       ctypes.c_uint]
        target = HWND_TOPMOST if on else HWND_NOTOPMOST
        return bool(fn(wintypes.HWND(hwnd), wintypes.HWND(target),
                       0, 0, 0, 0,
                       SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE))
    except Exception as e:
        log.debug("SetWindowPos 失败：%s", e)
        return False


# ══════════════════════════════════════════════════════════
#  窗口位置与尺寸
#
#  ★ 为什么不用 pywebview 的 window.move() / window.resize()
#
#    实测发现 pywebview 6.2.1 的 ``BrowserForm.move()`` 有 bug：
#    它给 SetWindowPos 的 cx / cy 传了 ``None``，
#    而 ctypes 默认把未声明类型的参数当 c_int，
#    ``None`` 转不成整数 → 直接抛
#        TypeError: 'NoneType' object cannot be interpreted as an integer
#
#    官方本意应该是想传「不改尺寸」（SWP_NOSIZE 已经声明了，
#    cx/cy 本来就该给 0）。这里自己调，顺手把 argtypes 写全，
#    避免 ctypes 猜类型。
# ══════════════════════════════════════════════════════════

def _set_window_pos(hwnd: int, x=None, y=None,
                    w=None, h=None) -> bool:
    """统一走这一个 —— 位置和尺寸都能改，不传的那部分保持不动。"""
    if not IS_WIN or not hwnd:
        return False
    try:
        #  ★ 用 WinDLL(use_last_error=True) —— windll 拿不到真错误码，
        #    返回 0 时全靠这个码排查（0.5.1 排拖动问题时加的）。
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        fn = u32.SetWindowPos
        fn.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                       ctypes.c_int, ctypes.c_int, ctypes.c_int,
                       ctypes.c_uint]
        fn.restype = wintypes.BOOL

        flags = SWP_NOZORDER | SWP_NOACTIVATE
        cx = cy = 0
        px = py = 0

        if x is None or y is None:
            flags |= SWP_NOMOVE
        else:
            px, py = int(x), int(y)

        if w is None or h is None:
            flags |= SWP_NOSIZE
        else:
            cx, cy = int(w), int(h)

        #  ★ HWND(0) 在 ctypes 里会被当成 NULL，
        #    而 SetWindowPos 的 hWndInsertAfter 收到 NULL 是合法的
        #    （配合 SWP_NOZORDER 时会被忽略），但保险起见传一个哨兵值：
        #    用 hwnd 自己，等价于「Z 序不变」。
        r = fn(wintypes.HWND(hwnd), wintypes.HWND(hwnd),
               px, py, cx, cy, flags)
        if not r:
            err = ctypes.get_last_error()
            log.debug("SetWindowPos 返回 0，错误码=%s（x=%s y=%s w=%s h=%s "
                      "flags=%#x）", err, px, py, cx or None, cy or None, flags)
            #  ★ 返回 0 不代表没生效 ——
            #    SetWindowPos 的成功判定是「非零」，
            #    但实测在带 SWP_NOMOVE/SWP_NOSIZE 的组合下
            #    有时会返回 0 而操作其实已经生效。
            #    所以这里复查一下实际位置，对上了就当成成功。
            try:
                cur = wintypes.RECT()
                ctypes.windll.user32.GetWindowRect(wintypes.HWND(hwnd),
                                                   ctypes.byref(cur))
                if x is not None and y is not None:
                    if abs(cur.left - px) <= 2 and abs(cur.top - py) <= 2:
                        return True
                if w is not None and h is not None:
                    if (abs((cur.right - cur.left) - cx) <= 2
                            and abs((cur.bottom - cur.top) - cy) <= 2):
                        return True
            except Exception:
                pass
        return bool(r)
    except Exception as e:
        log.debug("_set_window_pos 失败：%s", e)
        return False


def move_window(window, x: int, y: int) -> bool:
    """把窗口移到 (x, y)。

    ★ 传进来的 x/y 用**物理像素** —— 和 work_area() 一套。
      实际算的时候用 ``top_right_position()`` 配合 ``client_size()``，
      三个函数都是物理，不会错位。
    """
    hwnd = hwnd_of(window)
    if not hwnd:
        return False
    return _set_window_pos(hwnd, x=int(round(x)), y=int(round(y)))


def resize_window(window, width: int, height: int) -> bool:
    """改变窗口尺寸。

    ★ 这里的 width/height 是**用来算屏幕上占多大**的物理尺寸。
      界面里谈的「宽 340」是 CSS 像素，
      物理尺寸 = CSS 像素 × 缩放倍率。
      所以调用方（widget.py）会先乘好再传进来。
    """
    hwnd = hwnd_of(window)
    if not hwnd:
        return False
    return _set_window_pos(hwnd, w=int(round(width)), h=int(round(height)))


def css_to_physical(window, css_px: int) -> int:
    """把 CSS 像素尺寸换算成 SetWindowPos 要的尺寸。

    ★ 这里**只乘 DPI 倍率** —— 实测得出的结论：

      · 无边框窗口的「窗口尺寸」和「客户区尺寸」是**相等**的
        （实测 914x1489 == 914x1489，因为没有边框可差）
      · pywebview 建窗时用的是 ``Size = CSS 值 × DPI``
      · 它自己那个 ``window.resize(600, 600)`` 实测量出
        ``1200x1200`` —— 也是「CSS × DPI」，和这里一致

    ★ 早先这里写错过一次，说明白免得再踩：
      当时想「用当前窗口宽 ÷ 某个 CSS 值反推倍率」，
      可那个 CSS 值是**调用方随便传的**（比如 300），
      而窗口当时是 914 宽 → 算出「倍率 3.05」并缓存下来。
      之后所有换算都乘 3.05，全错。
      倍率只有一个正确来源：DPI。
    """
    if css_px <= 0:
        return css_px
    s = dpi_scale(window) or 1.0
    return int(round(css_px * s))


def client_size(window) -> tuple[int, int]:
    """窗口客户区尺寸，**物理像素**。

    ★ 这里**不做** DPI 除法 —— 实测出来的结论：

        pywebview 启动时会改整个进程的 DPI 感知
        （DPI_AWARENESS_CONTEXT 从 24592 变成 49169）。
        之后：
          · GetClientRect / native.Width     → 物理
          · SystemParametersInfo(工作区)      → 也是物理
          · GetDpiForSystem()                → 192（2.00x）
        两边同一套坐标系，直接用即可。

        启动之前则相反：系统会帮忙把值缩放成「逻辑」的
        （报 1280x752，而真实屏幕是 2560x1504）。

        早先我在这里除了一个 scale，结果
        「逻辑窗口 340」配「物理屏幕 2560」→ 算到屏幕外面去。

    注意：建窗**之前**要摆位置的话，那时拿到的还是逻辑值，
    正好和 ``create_window(x=, y=)`` 的期望一致 —— 不用换算。
    """
    hwnd = hwnd_of(window)
    if not hwnd:
        return 0, 0
    w, h = _client_size(hwnd)
    return (w, h) if (w > 0 and h > 0) else (0, 0)


# ══════════════════════════════════════════════════════════
#  DPI
#
#  ★ 核心结论（实测得出的，不是照抄文档）：
#
#      pywebview 启动时会改整个进程的 DPI 感知上下文
#      （实测 24592 → 49169，也就是切到「每显示器感知」）。
#
#      启动**之前**：
#          系统帮忙缩放，我们看到的都是「逻辑」值
#          （屏幕报 1280x752，实际物理是 2560x1504）
#
#      启动**之后**：
#          一切都是「物理」值，和真实像素 1:1
#          （屏幕报 2560x1504，GetDpiForSystem 报 192）
#
#      所以运行期**不要**再做乘除换算 —— 两边本来就同一套。
#
#      只有一种情况要注意：在 pywebview 启动前算窗口位置，
#      那批值是逻辑的，而 ``create_window(x=, y=)`` 正好也收逻辑值，
#      直接传即可。
# ══════════════════════════════════════════════════════════

def dpi_scale(window=None) -> float:
    """当前的 DPI 缩放倍率 —— **只用来显示/调试**，别拿去换算坐标。

    pywebview 启动后会返回 2.0 这类值，但那时坐标已经是物理的了，
    乘上去反而错。想显示「界面看起来多大」可以用它。
    """
    #  ① pywebview 内部算好的
    try:
        n = getattr(window, "native", None)
        if n is not None:
            s = getattr(n, "_scale", None)
            if isinstance(s, (int, float)) and s > 0:
                return float(s)
    except Exception:
        pass

    #  ② 自己问系统
    if IS_WIN:
        try:
            hwnd = hwnd_of(window) if window is not None else 0
            if hwnd:
                dpi = ctypes.windll.user32.GetDpiForWindow(
                    wintypes.HWND(hwnd))
                if dpi:
                    return float(dpi) / 96.0
        except Exception as e:
            log.debug("GetDpiForWindow 失败：%s", e)

    #  ③ 退而求其次：主屏
    if IS_WIN:
        try:
            dc = ctypes.windll.user32.GetDC(0)
            dpi = ctypes.windll.gdi32.GetDeviceCaps(dc, 88)  # LOGPIXELSX
            ctypes.windll.user32.ReleaseDC(0, dc)
            if dpi:
                return float(dpi) / 96.0
        except Exception:
            pass

    return 1.0


def is_dpi_aware() -> bool:
    """进程当前是不是「物理像素」模式（也就是 pywebview 起来之后）。

    位置计算要不要换算，看这个。正常运行时为 True。
    """
    if not IS_WIN:
        return False
    try:
        return dpi_scale() > 1.01
    except Exception:
        return False




# ══════════════════════════════════════════════════════════
#  自绘标题栏（无边框）
# ══════════════════════════════════════════════════════════

def set_frameless(window) -> bool:
    """去掉系统标题栏，改由界面自己画按钮。

    pywebview 的 ``frameless=True`` 在建窗时就设了，
    但如果在它之后又调了别的样式，边框可能回来。所以这里留一个补丁口。

    实测注意：WinForms 的 ``FormBorderStyle=None`` 之后
    **窗口的拖动要自己实现**（前端的 -webkit-app-region 有效）。

    ★★ 0.5.2 修复：``FormBorderStyle=None`` 会**连带去掉**
       ``WS_MINIMIZEBOX``，于是窗口没法被最小化 ——
       用户反映「关掉置顶后也不能最小化，只能一直挡着」。
       最小化按钮在自绘标题栏上，能力必须保留，所以这里手动补回来。
    """
    native = getattr(window, "native", None)
    if native is None:
        return False
    ok = False
    try:
        from System.Windows.Forms import FormBorderStyle  # type: ignore
        #  注意：FormBorderStyle.None 在 Python 里不能写 ``.None``
        #  （None 是关键字），必须 getattr 取。
        none_val = getattr(FormBorderStyle, "None")
        native.FormBorderStyle = none_val
        ok = True
    except Exception as e:
        log.debug("设无边框失败：%s", e)

    #  补回最小化 / 可缩放（无边框模式会一并去掉）
    try:
        restore_minimize_capability(window)
    except Exception as e:
        log.debug("补最小化能力失败：%s", e)
    return ok


#  Win32 样式位
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
WS_THICKFRAME = 0x00040000
WS_SYSMENU = 0x00080000
GWL_STYLE = -16
SWP_FRAMECHANGED = 0x0020
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOZORDER = 0x0004


def restore_minimize_capability(window) -> bool:
    """确保窗口能被最小化 / 最大化（**不再补拖拽缩放**）。

    ★ 为什么需要
      ``FormBorderStyle=None``（无边框）时 WinForms 会把
      ``WS_MINIMIZEBOX`` / ``WS_MAXIMIZEBOX`` / ``WS_THICKFRAME``
      一起清掉。后果：
        · 界面上的「最小化」按钮调 ``window.minimize()`` 也无效——
          窗口根本不能进最小化状态
        · 关掉置顶之后，用户想把它最小化腾地方，做不到，
          只能一直挡着
      所以我们自己把这三个样式位补回去。

      补的是**能力**，不是外观 —— 无边框的效果不受影响
      （标题栏还是自己画的）。
    """
    if not IS_WIN:
        return False
    hwnd = hwnd_of(window)
    if not hwnd:
        return False
    try:
        u = ctypes.windll.user32
        u.GetWindowLongW.restype = ctypes.c_long
        u.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        u.SetWindowLongW.restype = ctypes.c_long
        u.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int,
                                     ctypes.c_long]
        u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND,
                                   ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int,
                                   ctypes.c_uint]
        style = u.GetWindowLongW(wintypes.HWND(hwnd), GWL_STYLE)
        #  只补「能不能最小化 / 最大化」，不碰缩放
        want = style | WS_MINIMIZEBOX | WS_MAXIMIZEBOX
        want &= ~WS_THICKFRAME
        if want == style:
            return True
        u.SetWindowLongW(wintypes.HWND(hwnd), GWL_STYLE, want)
        u.SetWindowPos(wintypes.HWND(hwnd), None, 0, 0, 0, 0,
                       SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER
                       | SWP_FRAMECHANGED)
        log.info("已补回最小化/最大化能力（不含拖拽缩放，0.5.2）")
        return True
    except Exception as e:
        log.debug("补样式失败：%s", e)
        return False

def set_shadow(window, on: bool = True) -> bool:
    """窗口投影（Windows 自带的那种）。

    无边框窗口没有阴影会显得「贴」在屏幕上，加上才有层次。
    """
    hwnd = hwnd_of(window)
    if not hwnd:
        return False
    if on:
        _extend_frame(hwnd, 1)
        return _dwm_set(hwnd, DWMWA_NCRENDERING_POLICY, 2)
    return _dwm_set(hwnd, DWMWA_NCRENDERING_POLICY, 1)


def set_dark_titlebar(window, dark: bool = True) -> bool:
    """标题栏深浅（跟随界面主题）。

    即使是无边框窗口也值得设 —— 系统弹的右键菜单会跟着变。
    """
    hwnd = hwnd_of(window)
    if not hwnd:
        return False
    return _dwm_set(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, 1 if dark else 0)


# ══════════════════════════════════════════════════════════
#  屏幕
# ══════════════════════════════════════════════════════════

def work_area() -> dict:
    """主屏可用区域（已扣任务栏），**物理像素**。

    ★ 返回值跟着调用时机变，这是实测确认的：
        pywebview 启动**之前** → 报「逻辑」值（1280x752）
        pywebview 启动**之后** → 报「物理」值（2560x1504）
      pywebview 启动时会把整个进程切成 DPI 感知模式。
    和 ``client_size()`` 是同一套口径，配起来用不会错。
    """
    if not IS_WIN:
        return {"x": 0, "y": 0, "w": 1280, "h": 800}
    try:
        rect = wintypes.RECT()
        #  SPI_GETWORKAREA = 0x0030，返回的就是扣掉任务栏的区域
        ok = ctypes.windll.user32.SystemParametersInfoW(
            0x0030, 0, ctypes.byref(rect), 0)
        if ok:
            return {"x": rect.left, "y": rect.top,
                    "w": rect.right - rect.left,
                    "h": rect.bottom - rect.top}
    except Exception as e:
        log.debug("取工作区失败：%s", e)
    return {"x": 0, "y": 0, "w": 1280, "h": 800}


def pin_to_corner(window, corner: str = "top-right",
                  margin_css: int = 16) -> dict:
    """把窗口贴到屏幕的某个角 —— **只移动，不改尺寸**。

    ★ 为什么只移动：
      改尺寸和摆位置合成一次调用时，「新尺寸」还没生效，
      拿来算位置就会偏到屏幕外。分成两步反而简单可靠 ——
      先调好大小，隔一下再调这个函数，量到的就是真实尺寸。

    尺寸一律现场量（``GetWindowRect``），不靠猜、不靠换算。
    """
    hwnd = hwnd_of(window)
    if not hwnd:
        return {"ok": False, "why": "拿不到窗口句柄"}

    try:
        r = wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(wintypes.HWND(hwnd),
                                           ctypes.byref(r))
        w_phys = r.right - r.left
        h_phys = r.bottom - r.top
    except Exception as e:
        return {"ok": False, "why": f"量不到窗口尺寸：{e}"}

    if w_phys <= 0 or h_phys <= 0:
        return {"ok": False, "why": "窗口尺寸是 0"}

    s = dpi_scale(window) or 1.0
    m = int(round(margin_css * s))
    wa = work_area()

    if corner == "top-right":
        x, y = wa["x"] + wa["w"] - w_phys - m, wa["y"] + m
    elif corner == "bottom-right":
        x, y = wa["x"] + wa["w"] - w_phys - m, wa["y"] + wa["h"] - h_phys - m
    elif corner == "top-left":
        x, y = wa["x"] + m, wa["y"] + m
    elif corner == "bottom-left":
        x, y = wa["x"] + m, wa["y"] + wa["h"] - h_phys - m
    else:                                   # center
        x = wa["x"] + (wa["w"] - w_phys) // 2
        y = wa["y"] + (wa["h"] - h_phys) // 2

    #  保险：别摆到屏幕外面去
    x = max(wa["x"], min(x, wa["x"] + wa["w"] - 40))
    y = max(wa["y"], min(y, wa["y"] + wa["h"] - 40))

    ok = _set_window_pos(hwnd, x=x, y=y)
    return {"ok": bool(ok), "x": x, "y": y,
            "w": w_phys, "h": h_phys,
            "scale": round(s, 2), "screen": f"{wa['w']}x{wa['h']}"}


def top_right_position(width: int, height: int, margin: int = 16) -> tuple[int, int]:
    """算出「右上角」的坐标（同样口径的像素）。

    仅在**同一套坐标系**里用 —— 和 work_area() 配套。
    """
    wa = work_area()
    return (wa["x"] + wa["w"] - width - margin,
            wa["y"] + margin)


def bottom_right_position(width: int, height: int, margin: int = 16) -> tuple[int, int]:
    wa = work_area()
    return (wa["x"] + wa["w"] - width - margin,
            wa["y"] + wa["h"] - height - margin)


# ══════════════════════════════════════════════════════════
#  一次性配置
# ══════════════════════════════════════════════════════════

def setup(window, glass: bool = True, rounded: bool = True,
          on_top: bool = False, dark_titlebar: bool = False,
          shadow: bool = True) -> dict:
    """把上面这些一次性配好，返回每项的结果（便于日志排查）。

    只在窗口 ``shown`` 之后调 —— 句柄那时才有效。
    """
    if not IS_WIN:
        return {"ok": False, "why": "非 Windows"}

    hwnd = hwnd_of(window)
    if not hwnd:
        return {"ok": False, "why": "拿不到窗口句柄"}

    out: dict = {"ok": True, "hwnd": hwnd, "win11": is_win11()}

    if shadow:
        out["shadow"] = set_shadow(window, True)
    if rounded:
        out["rounded"] = set_rounded(hwnd, "round")
    if glass:
        out["glass"] = apply_glass(window, "acrylic")

    #  ★ 0.5.2：无边框（FormBorderStyle=None）会把 WS_MINIMIZEBOX 等
    #    样式位一并去掉，导致「最小化按钮点了没反应」。
    #    这里无条件补回来 —— 补的是能力，不改外观。
    out["min_cap"] = restore_minimize_capability(window)

    #  置顶：**两种状态都要显式设**。
    #    只处理 on_top=True 的话，从「开着置顶」的配置启动时没问题，
    #    但一旦用户关掉，某些情况下窗口可能还留着 TOPMOST ——
    #    所以 False 时也要主动清一次（幂等，没坏处）。
    try:
        if on_top:
            out["on_top"] = set_always_on_top(window, True)
        else:
            set_always_on_top(window, False)
            out["on_top"] = False
    except Exception as e:
        out["on_top"] = False
        log.debug("设置顶失败：%s", e)

    out["dark_titlebar"] = set_dark_titlebar(window, dark_titlebar)

    log.info("窗体效果：%s", {k: v for k, v in out.items()
                              if k not in ("hwnd",)})
    return out


# ══════════════════════════════════════════════════════════
#  ★★★ 在 UI 线程上执行一段代码 —— 改 WinForms 属性必须走这里
#
#  【为什么（真机复现过死锁）】
#    WinForms 是**单线程**的：控件的属性只能在创建它那个线程
#    （UI 线程）上改。从别的线程直接写，会**死锁** ——
#    不是抛异常，是整个进程卡住，什么都动不了。
#
#    实测复现（_tools/repro_native_deadlock.py）：
#      在 js_api 的 handler 里执行一句
#          window.native.WindowState = FormWindowState.Maximized
#      进程立刻卡死，连看门狗定时器的打印都出不来。
#
#    这就是用户报的「点最大化崩溃 / 未响应」的真因。
#
#  【pywebview 自己怎么做】
#    看它 platforms/winforms.py 的 maximize：
#        def _maximize():
#            self.WindowState = WinForms.FormWindowState.Maximized
#        if self.InvokeRequired:
#            self.Invoke(Func[Type](_maximize))
#        else:
#            _maximize()
#    —— 用 WinForms 的 Control.Invoke 把代码排队到 UI 线程。
#    这里照抄这个思路。
#
#  【用法】
#      ui_thread_call(window, lambda n: setattr(n, "TopMost", True))
#
#    传进来的 fn 会收到 `window.native`。
#    ★ 如果拿不到 native（非 Windows / 窗口没建好），
#      就当成功返回 True —— 让调用方自己决定要不要报错。
# ══════════════════════════════════════════════════════════

def ui_thread_call(window, fn, timeout_ms: int = 4000) -> bool:
    """把 ``fn(native)`` 排到 UI 线程上执行。返回是否成功。

    ★★ 别在这个函数外面直接写 ``native.xxx = ...`` —— 会死锁。
    """
    n = getattr(window, "native", None)
    if n is None:
        return False

    #  ① 已经在 UI 线程上了 → 直接跑（绕开 Invoke 的开销）
    try:
        req = getattr(n, "InvokeRequired", None)
        if req is not None and not req:
            fn(n)
            return True
    except Exception as e:
        log.debug("ui_thread_call 直接执行失败：%s", e)

    #  ② 需要切线程 → 用 WinForms 的 Invoke
    try:
        from System import Action, Func, Type  # type: ignore
        from System.Windows.Forms import MethodInvoker  # type: ignore

        done = {"ok": False, "err": None}

        def work():
            try:
                fn(n)
                done["ok"] = True
            except Exception as exc:       # noqa: BLE001
                done["err"] = exc

        #  MethodInvoker 是 WinForms 给「无参无返回」的委托，
        #  比 Func[Type] 好构造，也更贴近这个用途。
        inv = MethodInvoker(work)
        n.Invoke(inv)

        if done["err"] is not None:
            log.debug("ui_thread_call 里的操作抛了：%s", done["err"])
        return bool(done["ok"])
    except Exception as e:
        log.debug("ui_thread_call 走 Invoke 失败，改回直接执行：%s", e)

    #  ③ 两条路都不行 → 退回直接执行
    #     这一步有死锁风险，但比「什么都不做强」。
    #     ★ 只在前面两步都失败时才走到这里 ——
    #       正常情况下走不到。
    try:
        fn(n)
        return True
    except Exception as e:
        log.debug("ui_thread_call 兜底也失败：%s", e)
        return False


# ══════════════════════════════════════════════════════════
#   尺寸限制
#
#   ★ 真凶在哪（实测出来的，别再去装 WM_GETMINMAXINFO 钩子了）：
#
#     pywebview 建窗时把 ``min_size`` 交给了 WinForms：
#
#         win.MinimumSize = System.Drawing.Size(min_w * scale,
#                                               min_h * scale)
#
#     实测：min_size=(370,500) + 2.0 缩放
#           → ``native.MinimumSize = {Width=740, Height=1000}``
#
#     WinForms 的 MinimumSize 是**自己拦**的：它在 ``SetBounds``
#     层面就把尺寸夹住了，根本轮不到 Windows 发 WM_GETMINMAXINFO。
#     所以一开始想用窗口过程钩子去改下限，方向就错了 ——
#     钩子装上了（``set_size_range`` 返回 True），
#     但 SetWindowPos 的请求在 WinForms 那一层就被吃掉了，
#     实测尺寸纹丝不动。
#
#     → 正解：直接改 ``native.MinimumSize``，简单可靠。
#
#   ★ 还有个坑：``MaximumSize = {Width=0, Height=0}``
#     在 WinForms 里**表示不限**（不是「最大为 0」）。
#     想看「到底限到多少」得判断 0 → 视为无上限。
# ══════════════════════════════════════════════════════════

#  当前这套限制（**CSS 像素**，给人看的）。None = 不限。
_SIZE_RANGE: dict = {"min": None, "max": None}


def _winforms_size(window):
    """拿 ``System.Drawing.Size`` 这个类型（拿不到就返回 None）。"""
    try:
        import clr  # noqa: F401
        from System.Drawing import Size  # type: ignore
        return Size
    except Exception:
        #  winforms 还没起来时可能取不到，退回 pywebview 那边的引用
        try:
            n = getattr(window, "native", None)
            if n is not None:
                return type(n.MinimumSize)
        except Exception:
            pass
        return None


def set_size_range(window, min_css=None, max_css=None) -> bool:
    """设置窗口能被拖到的尺寸范围（用 **CSS 像素** 说）。

    ``min_css`` / ``max_css`` 传 ``(宽, 高)`` 或 None（不限）。

    实测这条路是通的：
      · 完整界面 → ``set_size_range(win, (370, 500))``
      · 小浮窗   → ``set_size_range(win, (260, 240), (620, 900))``
    """
    if not IS_WIN:
        return False
    n = getattr(window, "native", None)
    if n is None:
        return False

    _SIZE_RANGE["min"] = tuple(min_css) if min_css else None
    _SIZE_RANGE["max"] = tuple(max_css) if max_css else None

    Size = _winforms_size(window)
    if Size is None:
        log.debug("拿不到 System.Drawing.Size，改不了尺寸范围")
        return False

    try:
        s = dpi_scale(window) or 1.0

        def work(nn):
            #  ★ 要乘 DPI —— WinForms 那对属性是**物理**像素，
            #    和 native.MinimumSize 里量到的一致（740 = 370 × 2）。
            if min_css:
                nn.MinimumSize = Size(int(round(min_css[0] * s)),
                                      int(round(min_css[1] * s)))
            else:
                nn.MinimumSize = Size(1, 1)

            if max_css:
                nn.MaximumSize = Size(int(round(max_css[0] * s)),
                                      int(round(max_css[1] * s)))
            else:
                #  0 = 不限（WinForms 的约定，不是「最大 0」）
                nn.MaximumSize = Size(0, 0)

        #  ★★ 必须走 ui_thread_call ——
        #    直接在 handler 线程写 MinimumSize 会**死锁**
        #    （原因见 ui_thread_call 的说明）
        ok = ui_thread_call(window, work)
        log.debug("尺寸范围已设为 最小 %s / 最大 %s（CSS 像素），"
                  "scale=%.2f，ui_thread=%s",
                  _SIZE_RANGE["min"], _SIZE_RANGE["max"], s, ok)
        return ok
    except Exception as e:
        log.debug("设尺寸范围失败：%s", e)
        return False


def size_range_now(window) -> dict:
    """读回当前的尺寸范围（**CSS 像素**），供诊断用。"""
    out = {"min": None, "max": None, "scale": None}
    n = getattr(window, "native", None)
    if n is None:
        return out
    try:
        s = dpi_scale(window) or 1.0
        out["scale"] = round(s, 2)
        mn, mx = n.MinimumSize, n.MaximumSize
        out["min"] = (round(mn.Width / s), round(mn.Height / s))
        #  WinForms：0 表示不限
        out["max"] = ((round(mx.Width / s), round(mx.Height / s))
                      if (mx.Width or mx.Height) else None)
    except Exception as e:
        out["error"] = str(e)
    return out


def resize_css(window, css_w: int, css_h: int) -> bool:
    """按 CSS 尺寸改窗口大小（左上角不动）。

    ★ 用 pywebview 自己的 ``window.resize()`` 而不是裸调
      ``SetWindowPos`` —— 实测这个才是好的：

        · ``window.resize(600, 600)`` → 客户区真的变成 1200x1200
          （它内部自己乘了 DPI，626 行附近）
        · 裸调 SetWindowPos 反而全被 WinForms 的 MinimumSize 吃掉，
          怎么调都不动

      尺寸换算的结论见 ``css_to_physical`` 的说明。
      改完建议隔一帧再调 ``pin_to_corner``，那时量到的就是新尺寸了。
    """
    #  ① 首选 pywebview 自己的接口
    if hasattr(window, "resize"):
        window.resize(int(css_w), int(css_h))
        return True

    #  ② 退路：自己调（尺寸按 CSS × DPI 算）
    hwnd = hwnd_of(window)
    if not hwnd:
        return False
    w = css_to_physical(window, css_w)
    h = css_to_physical(window, css_h)
    return _set_window_pos(hwnd, w=w, h=h)


def _cap_maximized_bounds(n) -> None:
    """把最大化边界框到屏幕**工作区**（不盖任务栏）。

    ★ 修的问题：无边框 WinForms 窗最大化时会铺满**整个屏幕**
      （1280x800），连任务栏一起盖掉 —— 有边框窗最大化时
      系统会自动让出任务栏，无边框的不会。
      WinForms 的 ``Form.MaximizedBounds`` 就是官方答案：
      它在窗体处理 WM_GETMINMAXINFO 时生效，
      不管最大化是 ShowWindow 还是 WindowState 触发的都认。
      ★ 必须在 UI 线程上设 —— 用 ``ui_thread_call`` 调进来，
        别在 js_api 的工作线程里直接调。
    """
    try:
        from System.Windows.Forms import Screen  # type: ignore
        n.MaximizedBounds = Screen.FromHandle(n.Handle).WorkingArea
    except Exception as e:
        log.debug("设 MaximizedBounds 失败：%s", e)


def maximize(window) -> bool:
    """最大化窗口（SW_MAXIMIZE），且不盖任务栏。

    ★ 为什么不用 pywebview 的 ``window.maximize()``：
      实测（0.5.1）在启动早期调它，WindowState 赋值完成了，
      窗口却落成**最小化**（-32000,-32000 那种经典状态）——
      启动时序和 WinForms 状态机有仇。ShowWindow 没有这层问题。
    """
    if not IS_WIN:
        return False
    hwnd = hwnd_of(window)
    if not hwnd:
        return False
    try:
        #  先框住最大化边界 —— 无边框窗的默认答案是整屏（含任务栏）
        ui_thread_call(window, _cap_maximized_bounds)
        ctypes.windll.user32.ShowWindow(wintypes.HWND(hwnd), 3)  # SW_MAXIMIZE
        return bool(ctypes.windll.user32.IsZoomed(wintypes.HWND(hwnd)))
    except Exception as e:
        log.debug("maximize 失败：%s", e)
        return False


def move_by(window, dx_css: int, dy_css: int) -> bool:
    """把窗口平移一段距离（**CSS 像素**，内部乘 DPI）。

    ★ 0.5.1 加它的原因：pywebview 内建拖动（.pywebview-drag-region
      → pywebviewMoveWindow → window.move）在这条链路上实测不生效
      （合成事件和真鼠标都试过，窗口纹丝不动），
      所以前端自己算位移增量、调这个接口，走 SetWindowPos。

    位置现场量（GetWindowRect 是物理像素），增量乘缩放倍率加上去。
    """
    if not IS_WIN:
        return False
    hwnd = hwnd_of(window)
    if not hwnd:
        return False
    try:
        r = wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(wintypes.HWND(hwnd),
                                           ctypes.byref(r))
        s = dpi_scale(window) or 1.0
        x = r.left + int(round(dx_css * s))
        y = r.top + int(round(dy_css * s))
        ok = _set_window_pos(hwnd, x=x, y=y)
        log.debug("move_by: hwnd=%s rect=(%s,%s) scale=%s → (%s,%s) ok=%s",
                  hwnd, r.left, r.top, s, x, y, ok)
        return ok
    except Exception as e:
        log.debug("move_by 失败：%s", e)
        return False


