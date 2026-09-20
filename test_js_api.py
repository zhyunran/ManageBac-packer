"""回归测试：确保 js_api 对象不会让 pywebview 递归爆炸。

用法：
    .venv\\Scripts\\python.exe test_js_api.py

为什么会需要这个测试？
─────────────────────
2026-09-18 的「未响应」事故根因：

    pywebview 注入 JS API 时，会遍历 js_api 对象的**所有公开属性**
    （`webview/util.py` 的 `get_functions`），遇到非函数对象就**递归进去**。

    我们把 `Api.window` 指向了 pywebview 的 `Window` 对象，于是递归进了
      · Window.dom      (DOM)
      · Window.events   (EventContainer)
      · Window.native   (.NET BrowserForm)
    而 `Window.width` / `Window.height` 又是**会 wait(15s) 的属性 getter**。

    结果：递归爆炸，实测 60 秒。
    期间 `pywebviewready` / `loaded` 事件全被堵住
    → 窗口显示出来了，但**永远「未响应」**。

    改名 `Api._window` 后：0.00 秒（下划线开头的属性会被跳过）。

本测试就是「再犯一次就立刻报警」。
"""
from __future__ import annotations

import inspect
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import webview  # noqa: E402

from widget import Api  # noqa: E402


def get_functions_like_pywebview(obj, base_name="", functions=None):
    """复刻 pywebview `webview/util.py` 里的 get_functions（含 exposed_objects）。"""
    exposed_objects: list[int] = []

    def get_args(func):
        return list(inspect.getfullargspec(func).args)

    def walk(o, base="", funcs=None):
        oid = id(o)
        if oid in exposed_objects:
            return funcs
        exposed_objects.append(oid)
        if funcs is None:
            funcs = {}
        for name in dir(o):
            try:
                full = f"{base}.{name}" if base else name
                if name.startswith("_"):
                    continue
                attr = getattr(o, name)
                if not getattr(attr, "_serializable", True):
                    continue
                if inspect.ismethod(attr) or inspect.isfunction(attr):
                    funcs[full] = get_args(attr)[1:]
                elif inspect.isclass(attr) or (isinstance(attr, object)
                    and not callable(attr)
                    and hasattr(attr, "__module__")
                ):
                    walk(attr, full, funcs)
            except Exception:
                continue
        return funcs

    return walk(obj, base_name, functions)


def main() -> int:
    print("=" * 66)
    print("  js_api 递归爆炸回归测试")
    print("=" * 66)

    api = Api()

    # ── 1) 静态检查：有没有公开的非函数属性 ──
    print("\n[1] 检查 Api 的公开成员 …")
    bad = []
    for name in dir(api):
        if name.startswith("_"):
            continue
        attr = getattr(api, name, None)
        if inspect.ismethod(attr) or inspect.isfunction(attr):
            print(f"    [OK]    方法 {name}")
            continue
        bad.append(f"{name}={type(attr).__name__}")
        print(f"    [危险]  非函数属性 {name}  ({type(attr).__name__})")

    if bad:
        print(f"\n  [X] 发现 {len(bad)} 个公开的非函数属性：{', '.join(bad)}")
        print("      pywebview 会递归遍历它们 → 窗口可能「未响应」。")
        print("      修法：改成下划线开头（如 self.window → self._window）")
        return 1
    print("    结论：只有方法公开 —— 安全")

    # ── 2) 动态检查：真实模拟一遍 pywebview 的遍历 ──
    print("\n[2] 模拟 pywebview 遍历（真实 Window 对象）…")
    w = webview.create_window("regression-test", html="<p>t</p>", js_api=api)
    api._window = w

    t0 = time.perf_counter()
    funcs = get_functions_like_pywebview(api)
    dt = time.perf_counter() - t0

    print(f"    耗时   : {dt:.3f} 秒")
    print(f"    条目数 : {len(funcs)}")

    if dt > 1.0:
        print(f"\n  [X] 遍历耗时 {dt:.2f} 秒 —— 太慢了！")
        print("      说明又有对象被递归进去了（阈值 1.0 秒）。")
        return 1
    if len(funcs) > 100:
        print(f"\n  [X] 条目数 {len(funcs)} 太多 —— 可能递归进了别的对象。")
        return 1

    print(f"    [OK] 耗时 {dt:.3f} 秒，条目 {len(funcs)} 条 —— 健康")

    # ── 3) 反向验证：故意挂一个 window 属性，必须变慢 ──
    print("\n[3] 反向验证：故意把对象挂成公开属性 window …")
    api2 = Api()
    w2 = webview.create_window("regression-test-2", html="<p>t</p>", js_api=api2)
    api2.window = w2          # ← 故意犯错

    t0 = time.perf_counter()
    funcs2 = get_functions_like_pywebview(api2)
    dt2 = time.perf_counter() - t0

    print(f"    耗时   : {dt2:.3f} 秒")
    print(f"    条目数 : {len(funcs2)}")

    if dt2 <= 1.0:
        print("\n  [!] 反向验证没变慢 —— 说明 pywebview 行为可能变了，")
        print("      请人工确认 util.py 的 get_functions 逻辑是否仍然如此。")
    else:
        print(f"    [OK] 复现了：慢了 {dt2 / max(dt, 1e-6):.0f} 倍"
              f"（{dt:.3f}s → {dt2:.3f}s）—— 证明这个坑是真的")

    print(f"\n{'=' * 66}")
    print("  [OK] 回归测试通过 —— js_api 不会再让窗口「未响应」")
    print(f"{'=' * 66}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
