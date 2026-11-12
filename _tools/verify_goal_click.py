"""交互验证：点一次「算一下」，看目标模拟是否真的出结果。

★ 这一步不能省 —— 语法通过 ≠ 功能正常。
  「元素渲染出来了」与「点了有用」是两件事。

做法：一次加载内点完按钮，用 MutationObserver 等结果出现，
      再把结果文字带回来（分两次加载状态会丢，看不到点击效果）。
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
new Promise(resolve => {
  const out = {steps: []};
  try { switchTab(2); } catch(e) {}

  const btns = [...document.querySelectorAll('[data-gsim]')];
  out.nBtns = btns.length;
  if(!btns.length){ out.err = '页面上没有「算一下」按钮'; resolve(out); return; }

  const b = btns[0];
  const key = b.dataset.gsim;
  out.course = key;
  const inp = document.getElementById('gtg-' + key);
  const box = document.getElementById('gsim-' + key);
  out.hasInput = !!inp;
  out.hasBox = !!box;
  if(!inp || !box){ out.err = '缺输入框或结果框'; resolve(out); return; }

  inp.value = '90';
  inp.dispatchEvent(new Event('input', {bubbles:true}));

  try { b.click(); } catch(e) { out.clickErr = String(e); }

  /*  轮询等结果 —— 无头模式下 promise 的落地时机不好预估，
      用 MutationObserver 容易在「算着呢…」那一下就被判为已完成。 */
  const t0 = Date.now();
  out.seen = [];
  const tick = () => {
    const t = (box.innerText || '').trim();
    if(t && out.seen[out.seen.length-1] !== t) out.seen.push(t.slice(0, 200));
    /*  出现「算着呢」之后的下一段就是结果 */
    if(t && !t.includes('算着呢')){
      out.text = t.slice(0, 400);
      resolve(out);
      return;
    }
    if(Date.now() - t0 > 12000){
      out.text = t.slice(0, 400);
      out.timeout = true;
      resolve(out);
      return;
    }
    setTimeout(tick, 150);
  };
  setTimeout(tick, 150);
})
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

    html = PREVIEW.read_text(encoding="utf-8")
    probe = f"""
<script>
window.addEventListener('load', () => {{
  setTimeout(async () => {{
    let payload;
    try {{
      payload = await ({JS});
    }} catch(e) {{
      payload = {{error: String(e)}};
    }}
    const d = document.createElement('div');
    d.id = '__probe__';
    d.textContent = JSON.stringify(payload);
    document.body.appendChild(d);
  }}, 1200);
}});
</script>
</body>"""
    tmp = Path(tempfile.gettempdir()) / "_preview_goal.html"
    tmp.write_text(html.replace("</body>", probe, 1), encoding="utf-8")

    r = subprocess.run(
        [edge, "--headless=new", "--disable-gpu",
         "--virtual-time-budget=15000", "--dump-dom", tmp.as_uri()],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120)
    dom = r.stdout or ""
    i = dom.find('id="__probe__"')
    if i < 0:
        print("[X] 探针没执行（页面可能有脚本错误）")
        print(dom[-1200:])
        return 1
    seg = dom[i:i + 6000]
    raw = seg.split(">", 1)[1].split("</div>", 1)[0]
    raw = (raw.replace("&quot;", '"').replace("&amp;", "&")
              .replace("&lt;", "<").replace("&gt;", ">")
              .replace("&#39;", "'"))
    try:
        d = json.loads(raw)
    except Exception as e:
        print("[X] 结果解析失败：", e)
        print(raw[:600])
        return 1

    print("=" * 68)
    print("  目标模拟 · 点击验证")
    print("=" * 68)
    print(f"  按钮数        : {d.get('nBtns')}")
    print(f"  课程          : {d.get('course')}")
    print(f"  输入框 / 结果框: {d.get('hasInput')} / {d.get('hasBox')}")
    if d.get("clickErr"):
        print(f"  点击异常      : {d['clickErr']}")
    txt = (d.get("text") or "").strip()
    print(f"\n  点击后显示：\n    {txt[:400] or '(空)'}")
    if d.get("seen"):
        print(f"\n  中间状态：{d['seen']}")

    ok = bool(txt) and not d.get("timeout") and not d.get("err") \
        and "算着呢" not in txt
    print("\n" + "=" * 68)
    print("  [OK] 点一下能算出结果" if ok else "  [X] 没有算出结果")
    print("=" * 68)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
