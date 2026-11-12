"""预览页验收 —— 用 Edge 无头模式跑一遍，客观核对新功能。

不做「看起来对不对」的判断，只断言可量化的东西：
  · 版本号、状态、覆盖范围字段是否送达前端
  · GPA 换算表是否由算法生成（不应再有 63/60 的旧档）
  · 图标是否都画出来了（0 尺寸 = 没渲染）
  · 徽章是否有值
  · 页面底部有没有裸露的代码文本

用法：python _tools/verify_preview.py（先跑 python make_preview.py 生成 _preview.html）
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
PREVIEW = ROOT / "_preview.html"

JS = r"""
JSON.stringify((() => {
  const out = {};
  /*  顶层 let 不在 window 上（踩过），得用 try 直接取；
      取不到就说明还没初始化完。 */
  let D = {};
  try { D = DATA || {}; } catch(e) { D = {}; }
  out.ver = document.querySelector('.ver')?.textContent || '';
  out.status = D.status || '';
  out.hasCoverage = !!(D.coverage && Object.keys(D.coverage).length);
  out.coverageKeys = D.coverage ? Object.keys(D.coverage) : [];
  out.lastAttemptAt = D.last_attempt_at ?? null;
  out.lastAttemptOk = D.last_attempt_ok ?? null;

  /*  GPA 页是切进去才渲染的 —— 先切过去，量完再切回来 */
  let before = 0;
  try { before = curTab; } catch(e) {}
  try { switchTab(2); } catch(e) {}
  const gs = [...document.querySelectorAll('.gscale span')].map(s => s.textContent.trim());
  out.gscale = gs;
  out.oldBands = gs.filter(s => /^(63|60)\+/.test(s));
  /*  目标模拟的入口（有类别权重就该出现） */
  out.goalBoxes = document.querySelectorAll('[data-gsim]').length;
  try { switchTab(before); } catch(e) {}

  /*  图标：只算「真的可见」的元素。
      折叠面板里的图标 boundingBox 本来就是 0（那是正常的），
      所以要跳过 display:none 祖先下的一切。 */
  const visible = el => {
    let n = el;
    while(n && n.nodeType === 1){
      const s = getComputedStyle(n);
      if(s.display === 'none' || s.visibility === 'hidden') return false;
      n = n.parentElement;
    }
    return true;
  };
  out.brokenIcons = [...new Set(
    [...document.querySelectorAll('svg.oi')]
      .filter(visible)
      .filter(s => { const b = s.getBoundingClientRect();
                     return b.width < 8 || b.height < 8; })
      .map(s => (s.querySelector('use')?.getAttribute('href') || '') + '')
  )];

  out.hasChartSymbol = !!document.getElementById('i-chart');
  out.badges = {};
  ['tasks','sch','gpa','files','tm'].forEach(k => {
    const el = document.getElementById('badge-' + k);
    out.badges[k] = el ? el.textContent : null;
  });
  out.leakedCode = document.body.innerText.includes('const ');
  out.secAnchors = document.querySelectorAll('[data-anchor]').length;
  return out;
})())
"""


def main() -> int:
    if not PREVIEW.exists():
        print("[X] 没有 _preview.html —— 先跑 make_preview.py")
        return 2

    edge = None
    for c in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
        if Path(c).exists():
            edge = c
            break
    if not edge:
        print("[X] 找不到 Edge")
        return 2

    #  页面里 DATA 是顶层 let（不在 window 上），所以要注入一段
    #  取值的脚本 —— 这里用 --dump-dom 拿不到变量，改成临时包一层。
    html = PREVIEW.read_text(encoding="utf-8")
    probe = f"""
<script>
window.addEventListener('load', () => {{
  setTimeout(() => {{
    try {{
      const r = {JS};
      const d = document.createElement('div');
      d.id = '__probe__';
      d.textContent = r;
      document.body.appendChild(d);
    }} catch(e) {{
      const d = document.createElement('div');
      d.id = '__probe__';
      d.textContent = JSON.stringify({{error: String(e)}});
      document.body.appendChild(d);
    }}
  }}, 1500);
}});
</script>
</body>"""
    tmp = Path(tempfile.gettempdir()) / "_preview_probe.html"
    tmp.write_text(html.replace("</body>", probe, 1), encoding="utf-8")

    r = subprocess.run(
        [edge, "--headless=new", "--disable-gpu", "--virtual-time-budget=8000",
         "--dump-dom", tmp.as_uri()],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=90)

    dom = r.stdout or ""
    i = dom.find('id="__probe__"')
    if i < 0:
        print("[X] 探针没执行（页面可能有脚本错误）")
        print(dom[-1500:])
        return 1
    seg = dom[i:i + 4000]
    raw = seg.split(">", 1)[1].split("</div>", 1)[0]
    raw = (raw.replace("&quot;", '"').replace("&amp;", "&")
              .replace("&lt;", "<").replace("&gt;", ">"))
    try:
        d = json.loads(raw)
    except Exception as e:
        print("[X] 探针结果解析失败：", e)
        print(raw[:800])
        return 1

    bad = 0

    def chk(ok: bool, label: str, got=None) -> None:
        nonlocal bad
        print(f"  {'[OK]' if ok else '[X] '} {label}"
              + ("" if ok else f"  → {got!r}"))
        if not ok:
            bad += 1

    print("=" * 68)
    print("  预览页验收")
    print("=" * 68)

    #  期望版本不写死：从 pyproject.toml 读，避免版本一升这个检查就误报
    import re as _re
    _want = ""
    try:
        _m = _re.search(r'^\s*version\s*=\s*"([^"]+)"',
                        (ROOT / "pyproject.toml").read_text(encoding="utf-8"), _re.M)
        _want = f"v{_m.group(1)}" if _m else ""
    except Exception:
        pass
    if _want:
        chk(d.get("ver") == _want, f"版本号是 {_want}", d.get("ver"))
    else:
        print(f"  [i] 版本号 = {d.get('ver')!r}（未能从 pyproject.toml 读出期望值）")

    print("\n  ── 数据可信度（B 批）──")
    chk(d.get("hasCoverage"), "覆盖范围字段已送达",
        d.get("coverageKeys"))
    chk(d.get("lastAttemptAt") is not None, "双时间轴字段已送达",
        d.get("lastAttemptAt"))
    chk(d.get("status") in ("ok", "empty", "partial", "unread",
                            "idle", "running", "need_login"),
        "状态值在七态之内", d.get("status"))

    print("\n  ── GPA 换算表（A2）──")
    n = len(d.get("gscale") or [])
    chk(n == 11, f"换算表 {n} 档（10 档 + 兜底）")
    chk(not d.get("oldBands"), "没有 63/60 的旧档", d.get("oldBands"))
    chk((d.get("goalBoxes") or 0) > 0,
        f"目标模拟入口已渲染（{d.get('goalBoxes')} 个）")

    print("\n  ── 图标（A3）──")
    chk(d.get("hasChartSymbol"), "i-chart 图标已定义")
    chk(not d.get("brokenIcons"), "没有画不出来的图标",
        d.get("brokenIcons"))

    print("\n  ── 徽章（A4）──")
    b = d.get("badges") or {}
    chk(b.get("gpa") not in (None, "", "—"),
        f"GPA 徽章有值：{b.get('gpa')}")
    print(f"     全部徽章：{b}")

    print("\n  ── 阅读状态锚点（C1）──")
    chk((d.get("secAnchors") or 0) > 0,
        f"折叠区带了锚点标记（{d.get('secAnchors')} 个）")

    print("\n  ── 卫生检查 ──")
    chk(not d.get("leakedCode"), "页面底部没有裸露代码")

    print("\n" + "=" * 68)
    print(f"  {'[OK] 全部通过' if not bad else f'[X] {bad} 项没通过'}")
    print("=" * 68)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
