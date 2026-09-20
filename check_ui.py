"""UI 语法守卫 —— 改完 web/index.html 后跑一次。

【为什么需要（真实教训）】
    97KB 的 HTML 里嵌着 66KB 的 JS。只要有一个字符出错，
    整个界面就白屏 / 卡死，而且极难定位。
    我曾用「正则批量替换」去改这个文件，把代码块吃掉了，
    排查花了很久。这个脚本就是为了**当场发现**这类问题。

【用法】
    uv run check_ui.py

【原理】
    把内联脚本**内联**到一个临时 HTML 里，
    用系统自带的 Edge/Chrome 无头模式跑一遍 ——
    这是最权威的检查（就是真正的 JS 引擎）。
    报错行号会自动换算成 web/index.html 里的行号。

【约定】
     绝不使用正则/字符串替换批量修改 web/index.html
     只用精确的「替换某几行」方式编辑
     每次编辑后立刻运行本脚本
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HTML = ROOT / "web" / "index.html"


def find_browser() -> Path | None:
    cands = [
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    ]
    return next((c for c in cands if c.exists()), None)


def extract_scripts(html: str) -> list[tuple[str, int, str]]:
    """返回 [(脚本内容, 起始行号, 来源描述)]。

    支持两种写法：
      · <script> ... </script>          内联（老写法）
      · <script src="app.js"></script>  外链（拆文件之后）

     拆文件的动机：单文件 210KB 时，编辑器做大段替换会
      「报告成功但丢内容」（VS Code 提示差异算法提前停止）。
      拆开后每个文件几十 KB，编辑才可靠。

     阈值不能太高 —— 曾经因为正则误改，脚本被截断到 11KB，
      但更高的阈值会让它「看起来没有脚本」而漏检。
      100 字符以上就算。
    """
    out: list[tuple[str, int, str]] = []

    # ① 外链脚本
    for m in re.finditer(r"<script\s+src=[\"']([^\"']+)[\"']\s*>\s*</script>",
                         html, re.I):
        rel = m.group(1)
        p = (HTML.parent / rel).resolve()
        if not p.exists():
            out.append((f"/* 找不到文件 {rel} */", 1, f"缺失：{rel}"))
            continue
        body = p.read_text(encoding="utf-8")
        out.append((body, 1, rel))

    # ② 内联脚本
    for m in re.finditer(r"<script>(.*?)</script>", html, re.S):
        body = m.group(1)
        if len(body.strip()) < 100:
            continue
        start = html[: m.start(1)].count("\n") + 1
        out.append((body, start, f"index.html 内联（第 {start} 行起）"))

    return out


# 临时页面的前缀行数（脚本从第 7 行开始）—— 改模板时必须同步
PREFIX_LINES = 6


def browser_check(js: str, exe: Path, timeout: int = 60) -> dict | None:
    """用浏览器引擎检查。返回 {line, col, msg} 或 None（无错）。"""
    page = ("<!DOCTYPE html>\n"                                   # 1
        "<html><head><meta charset='utf-8'></head><body>\n"    # 2
        "<pre id='o'>?</pre>\n"                               # 3
        "<script>\n"                                          # 4
        "window.__E__=null;"
        "window.onerror=function(m,u,l,c){"
        "if(!window.__E__)window.__E__={m:String(m),l:l,c:c};return true;};\n"
        "</script>\n"                                         # 6
        "<script>\n" + js + "\n</script>\n"                   # 7 ..
        "<script>document.getElementById('o').textContent="
        "JSON.stringify(window.__E__);</script>\n"
        "</body></html>"
    )

    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "t.html"
        f.write_text(page, encoding="utf-8")
        try:
            r = subprocess.run([str(exe), "--headless=new", "--disable-gpu",
                 "--no-sandbox", "--dump-dom", f.as_uri()],
                capture_output=True, encoding="utf-8", errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return {"line": 0, "col": 0, "msg": "浏览器超时（脚本可能死循环）"}
        except Exception as e:
            return {"line": 0, "col": 0, "msg": f"启动浏览器失败：{e}"}

        m = re.search(r'<pre id="o">(.*?)</pre>', r.stdout or "", re.S)
        if not m:
            return None
        txt = m.group(1).strip()
        if txt in ("?", "null", "", "undefined"):
            return None
        try:
            d = json.loads(txt)
        except Exception:
            return None
        page_line = int(d.get("l") or 0)
        js_line = max(1, page_line - PREFIX_LINES)
        msg = d.get("m", "未知错误")

        #  过滤「因为临时页面缺少 DOM 元素」而产生的运行时错误。
        #   我们只关心**语法错误**和「脚本解析失败」，那些才是白屏的原因。
        #   形如：Cannot read properties of null (reading 'addEventListener')
        #        Cannot set properties of null ...
        #        document.getElementById(...) is null
        if "SyntaxError" not in msg:
            low = msg.lower()
            looks_dom = ("cannot read properties of null" in low
                or "cannot set properties of null" in low
                or "is null" in low
                or "of undefined" in low
                or "not defined" in low
            )
            # 只把「第一行就崩」当作问题；中途崩通常是缺 DOM 造成
            if looks_dom and js_line > 1:
                return None

        return {"line": js_line, "col": d.get("c"), "msg": msg}


def main() -> int:
    if not HTML.exists():
        print(f"[X] 找不到 {HTML}")
        return 2

    html = HTML.read_text(encoding="utf-8")
    scripts = extract_scripts(html)

    print("=" * 68)
    print("  UI 语法守卫")
    print("=" * 68)
    print(f"  文件     : {HTML}")
    print(f"  大小     : {len(html):,} 字符 / {html.count(chr(10)) + 1:,} 行")
    print(f"  内联/外链脚本 : {len(scripts)} 个")
    if scripts:
        js, base, srcname = max(scripts, key=lambda x: len(x[0]))
        print(f"  主脚本   : {len(js):,} 字符 / "
              f"{js.count(chr(10)) + 1:,} 行（{srcname}）")
    print()

    exe = find_browser()
    if exe is None:
        print("  [!] 本机没找到 Edge/Chrome，无法做权威校验")
        print("      请手动打开界面确认：uv run widget.py")
        return 0
    print(f"  浏览器   : {exe.name}")
    print()

    bad = 0
    for i, (js, base, srcname) in enumerate(scripts):
        print(f"  ── 检查 #{i + 1}：{srcname}（{len(js):,} 字符）──")
        r = browser_check(js, exe)
        if r is None:
            print("     [OK] 无语法错误")
            continue
        bad += 1
        if r["line"]:
            real_line = base + r["line"] - 1 if base > 1 else r["line"]
            where = (f"HTML 第 {real_line} 行" if base > 1
                     else f"文件第 {real_line} 行")
            print(f"     [X] 脚本内第 {r['line']} 行（{where}）第 {r['col']} 列")
            print(f"         {r['msg']}")
            lines = js.split("\n")
            for k in range(max(0, r["line"] - 3),
                           min(len(lines), r["line"] + 2)):
                mark = " <<< 这里" if k == r["line"] - 1 else ""
                print(f"        {k + 1:5} | {lines[k][:100]}{mark}")
        else:
            print(f"     [X] {r['msg']}")

    print()
    print("=" * 68)
    if bad:
        print(f"  [X] {bad} 个脚本有语法错误 —— 界面会白屏，请先修复")
        return 1
    print("  [OK] 界面脚本语法正常")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
