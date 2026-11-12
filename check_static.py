"""界面静态检查（不联网、不起浏览器）。

检查两件每次都靠人眼发现的事：

  ① 图标一致性 —— `icon('x')` 用的每个名字，`index.html` 里都得有
     对应的 `<symbol id="x">`。少了就是一块空白，界面上看不出来。
     0.5.1 的「分类占比」标题就栽在这上面（用了不存在的 i-chart）。

  ② 同名函数 —— 同一个文件里 `function foo(` 出现两次，后一个会
     悄悄盖掉前一个。项目里已经栽过两次（pipeline.auto_status 的
     KeyError、web 里 apiListFiles 的新旧两份），所以扫一遍。

用法：
    python check_static.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent
HTML = ROOT / "web" / "index.html"
JS = ROOT / "web" / "app.js"

bad = 0


def chk(ok: bool, label: str) -> None:
    global bad
    print(f"  {'[OK]' if ok else '[X] '} {label}")
    if not ok:
        bad += 1


def main() -> int:
    print("=" * 68)
    print("  静态检查（图标 / 重名函数）")
    print("=" * 68)

    if not HTML.exists() or not JS.exists():
        print("  找不到 web/index.html 或 web/app.js")
        return 2

    html = HTML.read_text(encoding="utf-8")
    js = JS.read_text(encoding="utf-8")

    # ── ① 图标 ──
    symbols = set(re.findall(r'<symbol\s+id="([^"]+)"', html))
    used: set[str] = set(re.findall(r"icon\(\s*'([^']+)'", js))
    used |= set(re.findall(r'href="#(i-[^"]+)"', html))
    used |= set(re.findall(r'<use\s+href="#([^"]+)"', html))

    missing = sorted(used - symbols)
    unused = sorted(symbols - used - {"i-x"})   # i-x 常在模板串里用

    print(f"\n  ── ① 图标 ──")
    print(f"     图标库 {len(symbols)} 个 · 代码引用 {len(used)} 个")
    chk(not missing, "引用的图标都有定义"
        + (f"（缺：{', '.join(missing)}）" if missing else ""))
    if unused:
        print(f"     [i] 未引用（不算错，可能是备用）：{', '.join(unused)}")

    # ── ② 同名函数 ──
    print(f"\n  ── ② 同名函数 ──")
    found = re.findall(
        r"^\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", js, re.M)
    seen: dict[str, int] = {}
    for name in found:
        seen[name] = seen.get(name, 0) + 1
    dupes = sorted(n for n, c in seen.items() if c > 1)
    print(f"     函数声明 {len(found)} 个 · 去重后 {len(seen)} 个")
    chk(not dupes, "没有重复定义"
        + (f"（重复：{', '.join(dupes)}）" if dupes else ""))

    # ── ③ 换算表一致（顺手查一把）──
    print(f"\n  ── ③ GPA 换算表 ──")
    m = re.search(r"const GPA_BANDS\s*=\s*\[(.*?)\];", js, re.S)
    if m:
        pairs = re.findall(r"\[(\d+),\s*([\d.]+)\]", m.group(1))
        lows = [int(p[0]) for p in pairs]
        py = (ROOT / "app" / "gpa.py").read_text(encoding="utf-8")
        pyb = re.search(r"PERCENT_BANDS[^=]*=\s*\[(.*?)\]", py, re.S)
        #  0 是两边的「兜底档」，不参与比对（前端用 return 0.0 表达）
        py_lows = [int(x) for x in re.findall(r"\((\d+),", pyb.group(1))] \
            if pyb else []
        py_lows = [x for x in py_lows if x > 0]
        js_lows = [x for x in lows if x > 0]
        chk(js_lows == sorted(py_lows, reverse=True),
            f"前端 GPA_BANDS {js_lows} 与后端 gpa.py {sorted(py_lows, reverse=True)} 一致")
    else:
        chk(False, "没找到 GPA_BANDS（换算表改过名字？）")

    print("\n" + "=" * 68)
    if bad:
        print(f"  [X] {bad} 项没通过")
    else:
        print("  [OK] 全部通过")
    print("=" * 68)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
