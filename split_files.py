"""把 web/index.html 拆成 CSS / JS 多个文件。

 为什么要拆
  这个文件已经 210KB（CSS 74KB + JS 122KB）。
  实测症状：用编辑器做大段替换时**报告成功但内容被丢弃**，
  VS Code 提示「差异算法 5000ms 提前停止」——
  文件太大，diff 计算超时后放弃了。

  拆开后：
    · 每个文件只有几十 KB，编辑可靠
    · 改 CSS 不用碰 JS，改 JS 不用碰 CSS
    · 浏览器缓存也能分开

 拆法（拆完仍是**同一份** index.html 的工作方式，零构建步骤）
    web/index.html      只留骨架 + SVG 图标库
    web/app.css         <style> 里的全部内容
    web/app.js          主脚本

 相关改动
    check_ui.py 已同步支持 <script src="..."> 形式
"""
import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent      # 本脚本放在项目根
WEB = ROOT / "web"
SRC = WEB / "index.html"

assert SRC.exists(), f"找不到 {SRC}"

html = SRC.read_text(encoding="utf-8")
print(f"原始 index.html: {len(html):,} 字符")

# 备份（万一拆坏了可以退回）
bak = WEB / "index.html.before_split"
if not bak.exists():
    bak.write_text(html, encoding="utf-8")
    print(f"已备份 → {bak.name}")

# ══════════ 1) 抽出 <style> ══════════
m_style = re.search(r"<style>(.*?)</style>", html, re.S)
assert m_style, "找不到 <style>"
css = m_style.group(1)

css_header = ("/* ManageBac-packer —— 样式表\n"
    " * 由 web/split_files.py 从 index.html 的 <style> 块抽出（2026-09-20）。\n"
    " * 拆开是为了避免单文件过大导致编辑器写入丢内容。\n"
    " */\n"
)
(WEB / "app.css").write_text(css_header + css.lstrip("\n"), encoding="utf-8")
print(f"  → app.css  {len(css):,} 字符")

stage1 = (html[: m_style.start()]
          + '<link rel="stylesheet" href="app.css">'
          + html[m_style.end():])

# ══════════ 2) 抽出主脚本 ══════════
marker = "<script>\nconst $ = s => document.querySelector(s);"
i = stage1.find(marker)
assert i >= 0, "找不到主脚本起点"
j = stage1.find("</script>", i)
assert j >= 0, "找不到主脚本结尾"

js = stage1[i + len("<script>\n"): j]

js_header = ("/* ManageBac-packer —— 主逻辑\n"
    " * 由 web/split_files.py 从 index.html 内联脚本抽出（2026-09-20）。\n"
    " */\n"
)
(WEB / "app.js").write_text(js_header + js, encoding="utf-8")
print(f"  → app.js   {len(js):,} 字符")

stage2 = (stage1[:i]
          + '<script src="app.js"></script>'
          + stage1[j + len("</script>"):])

SRC.write_text(stage2, encoding="utf-8")
print(f"新 index.html: {len(stage2):,} 字符")
print("完成 —— 记得跑 check_ui.py 验证")
