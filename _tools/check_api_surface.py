"""API 面守卫 —— 核对「后端暴露了什么」与「前端叫了什么」。

解决的结构性问题
----------------
`widget.py` 的 `Api` 类对外暴露几十个方法（pywebview 会把**所有非下划线开头**
的成员都挂到 `window.pywebview.api` 上，见 webview/util.py 的 get_functions）。
项目里没有任何地方能一眼看出「哪些是活的、哪些是死的」，于是：

  · 死方法越攒越多，维护者不敢删；
  · 前端写错名字（或后端改名）时不会有任何报错，界面上表现为「按钮点了没反应」。

这个守卫把两侧对齐后给出一张表，并区分三类：
    DEAD_BACKEND   后端暴露、前端从不调用   → 要么接上界面，要么删
    MISSING        前端调用、后端没有       → 运行时会直接失败，必须修
    PRIVATE_LEAK   以下划线开头却被前端调用 → pywebview 不暴露它，必错

前端有三种访问形态，少认一种就会把活方法误判成死的（实测踩过）：
    ① 直连      window.pywebview?.api?.ec_watch_scan
    ② 别名变量  const a = window.pywebview.api;  a.win_drag(dx, dy)
    ③ 字符串分发 winApi('minimize') → 内部 a[name](...)

用法
----
    python _tools/check_api_surface.py            # 报告
    python _tools/check_api_surface.py --strict   # 有 MISSING/PRIVATE_LEAK 就非 0 退出
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
WIDGET = ROOT / "widget.py"
WEB = ROOT / "web"

#  直接命名 api 的形态：pywebview.api.foo / api['foo'] / winApi('foo')
RE_DIRECT = re.compile(r"pywebview\s*\??\.\s*api\s*\??\.\s*([A-Za-z_]\w*)")
RE_INDEX = re.compile(r"pywebview\s*\??\.\s*api\s*\[\s*['\"]([A-Za-z_]\w*)['\"]\s*\]")
RE_STRING_DISPATCH = re.compile(r"winApi\s*\(\s*['\"]([A-Za-z_]\w*)['\"]")

#  从 pywebview 赋值出来的变量名（含 const/let/var 与裸赋值）。
#  ★ 右侧必须同时出现 `pywebview` 与 `.api`，说明这个变量真的装的是 api 对象。
#    两种常见写法都要覆盖：
#        const a = window.pywebview && window.pywebview.api;     ← 带短路守卫
#        const a = window.pywebview?.api;                        ← 可选链
#    若只要求「右侧含 pywebview」，`a = [...].map(...)` 这类数组链也会被收进来，
#    于是 `.map/.slice` 被误算成后端方法（实测踩过）。
RE_ALIAS_TAIL = r"[^;\n]*pywebview[^;\n]*\.\s*api\b[^;\n]*"
RE_ALIAS_DECL = re.compile(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*" + RE_ALIAS_TAIL)
RE_ALIAS_BARE = re.compile(r"(?:^|[;{}\n])\s*([A-Za-z_$][\w$]*)\s*=\s*" + RE_ALIAS_TAIL)


#  别名变量一旦被复用（先装 api，后来又被赋成数组），它上面的
#  JS 内置方法就会混进来。这些名字不可能出现在后端 Api 上，直接忽略。
#  只列真正的 JS 内置方法名，不放宽到「猜」。
JS_BUILTINS = {
    "map", "filter", "slice", "splice", "concat", "join", "push", "pop",
    "shift", "unshift", "forEach", "reduce", "find", "findIndex", "some",
    "every", "includes", "indexOf", "sort", "reverse", "flat", "keys",
    "values", "entries", "trim", "split", "replace", "toString", "valueOf",
    "then", "catch", "finally", "charAt", "startsWith", "endsWith", "padStart",
}


def api_class_methods(private: bool) -> dict[str, int]:
    """widget.py 的 Api 类方法（private=True 取下划线开头的那批）。

    ★ 双下划线（`__init__` 等）既不是公开 API 也不算「私有方法」——
      pywebview 的 get_functions 会跳过所有下划线开头的名字，
      而 `__init__` 是构造器，两条都不该进任何一类（否则会误报「死 API」）。
    """
    tree = ast.parse(WIDGET.read_text(encoding="utf-8", errors="replace"))
    out: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Api":
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    name = item.name
                    if name.startswith("__") and name.endswith("__"):
                        continue
                    if name.startswith("_") == private:
                        out[name] = item.lineno
    return out


def frontend_calls() -> dict[str, int]:
    """前端调用到的 api 名字 → 首次出现的行号。

    ★ 别名必须**先证明它装的是 api 对象**：单字母 `a` 在别处也可能是数组，
      只看 `a.map(...)` 会把 JS 内置的 map 误算成后端方法（实测踩过）。
    """
    out: dict[str, int] = {}

    def note(name: str, line: int) -> None:
        if name and name not in out:
            out[name] = line

    for f in sorted(list(WEB.glob("*.js")) + list(WEB.glob("*.html"))):
        text = f.read_text(encoding="utf-8", errors="replace")
        line_of = lambda pos: text[:pos].count("\n") + 1  # noqa: E731

        for rx in (RE_DIRECT, RE_INDEX, RE_STRING_DISPATCH):
            for m in rx.finditer(text):
                note(m.group(1), line_of(m.start()))

        aliases = set(RE_ALIAS_DECL.findall(text)) | set(RE_ALIAS_BARE.findall(text))
        for var in aliases:
            for m in re.finditer(rf"\b{re.escape(var)}\s*\.\s*([A-Za-z_]\w*)\s*\(", text):
                note(m.group(1), line_of(m.start()))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true",
                    help="有 MISSING 或 PRIVATE_LEAK 时返回非 0")
    args = ap.parse_args()

    pub = api_class_methods(private=False)
    priv = api_class_methods(private=True)
    calls = frontend_calls()

    missing = {k: v for k, v in calls.items()
               if k not in pub and k not in priv and k not in JS_BUILTINS}
    leak = {k: v for k, v in calls.items() if k in priv}
    dead = {k: v for k, v in pub.items() if k not in calls}
    live = {k: v for k, v in pub.items() if k in calls}

    print("=" * 72)
    print("  API 面守卫（后端暴露 vs 前端调用）")
    print("=" * 72)
    print(f"  后端公开方法  : {len(pub)}")
    print(f"  后端私有方法  : {len(priv)}（pywebview 不暴露）")
    print(f"  前端调用的名字: {len(calls)}")
    print()
    print(f"  活的            : {len(live)}")
    print(f"  后端有、前端不用: {len(dead)}")
    print(f"  前端用、后端没有: {len(missing)}")
    print(f"  调了下划线私有项: {len(leak)}")

    if missing:
        print("\n  ── [X] 前端调用但后端不存在（运行时会失败）──")
        for k, v in sorted(missing.items()):
            print(f"      {k}   (web 行 {v})")
    else:
        print("\n  [OK] 前端没有调用不存在的后端方法")

    if leak:
        print("\n  ── [X] 前端调用了下划线私有方法（pywebview 不暴露）──")
        for k, v in sorted(leak.items()):
            print(f"      {k}   (web 行 {v})  widget.py:{priv[k]}")
    else:
        print("  [OK] 没有调用私有方法")

    if dead:
        print(f"\n  ── 后端暴露但前端从不调用（{len(dead)} 个）──")
        print("     这些不是错，但没人知道它们是活的还是被忘了。")
        print("     处理方式二选一：接到界面上，或删掉。")
        for k, v in sorted(dead.items(), key=lambda kv: kv[1]):
            print(f"      widget.py:{v:<6} {k}")
    else:
        print("\n  [OK] 后端没有多余暴露的方法")

    bad = len(missing) + len(leak)
    print()
    print("=" * 72)
    print(f"  结论：有 {bad} 项必须修" if bad else "  结论：API 面无硬错误")
    print("=" * 72)
    return 1 if (args.strict and bad) else 0


if __name__ == "__main__":
    raise SystemExit(main())
