"""成绩库健康检查的回归测试（离线，在临时目录里跑）。

对应两处结构性问题：
  · grades.db 是成绩历史的**唯一副本**，却一直没有备份；
  · 写失败只写 log.debug（默认 INFO 下看不到），坏了也没人说。

本测试验证：
  ① 正常库能用，并且**当天会留一份 .bak**（热备，走 sqlite backup API）
  ② 库被写坏时 _connect() 抛错、DB_HEALTH 置为不健康 —— 不被当成「空历史」糊过去
  ③ 全过程不碰真实 %LOCALAPPDATA%\\ManageBac-packer\\data\\grades.db
"""
from __future__ import annotations

import hashlib
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = pathlib.Path(__file__).resolve().parent
REAL_DB = (pathlib.Path(os.environ.get("LOCALAPPDATA", ""))
           / "ManageBac-packer" / "data" / "grades.db")

PASS = 0
FAIL = 0


def chk(ok: bool, label: str, extra=None) -> None:
    global PASS, FAIL
    print(f"  {'[OK]' if ok else '[X] '} {label}" + ("" if ok else f"   → {extra!r}"))
    if ok:
        PASS += 1
    else:
        FAIL += 1


def _sha(p: pathlib.Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16] if p.exists() else "(无)"


CHILD = r'''
import sys, pathlib
root = pathlib.Path(sys.argv[1]); sys.path.insert(0, str(root))
from app import config, store

with store._connect() as c:
    c.execute("INSERT INTO snapshots (taken_at, overall_gpa, payload) VALUES (?,?,?)",
              ("2026-01-01T00:00:00", 3.5, "{}"))
assert store.DB_HEALTH["ok"] is True, "正常库被误判为不健康"
assert sorted(config.DATA_DIR.glob("grades.db.bak.*")), "没有留下当天 .bak"

config.DB_FILE.write_bytes(b"not a sqlite file" * 10)
store.DB_HEALTH.update(ok=True, note="")
raised = False
try:
    store._connect()
except Exception:
    raised = True
assert raised, "坏库没有抛错（会被当成空历史）"
assert store.DB_HEALTH["ok"] is False, "坏库没有把健康标记置为不健康"
assert "结构异常" in store.DB_HEALTH["note"], store.DB_HEALTH["note"]
print("CHILD_OK")
'''


def main() -> int:
    print("=" * 70)
    print("  成绩库健康检查回归")
    print("=" * 70)

    before = (_sha(REAL_DB), REAL_DB.stat().st_mtime_ns if REAL_DB.exists() else 0)

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="mbpk_dbchk_"))
    work = tmp / "src"
    skip = {".venv", "build", "dist", "release", "data", "logs",
            "__pycache__", "_recon", "_tools", "docs"}
    try:
        shutil.copytree(ROOT, work, ignore=lambda d, n: [x for x in n if x in skip])
        (tmp / "localappdata").mkdir(exist_ok=True)
        env = dict(os.environ)
        env["LOCALAPPDATA"] = str(tmp / "localappdata")

        child = tmp / "child.py"
        child.write_text(CHILD, encoding="utf-8")
        r = subprocess.run([sys.executable, str(child), str(work)],
                           env=env, cwd=str(work), capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=120)
        out = (r.stdout or "") + (r.stderr or "")

        chk("CHILD_OK" in out, "① 正常库可用 + 当天留下 .bak；② 坏库抛错并标记不健康",
            out.strip().splitlines()[-1] if out.strip() else "(无输出)")
    except subprocess.TimeoutExpired:
        chk(False, "子进程超时")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    after = (_sha(REAL_DB), REAL_DB.stat().st_mtime_ns if REAL_DB.exists() else 0)
    chk(before == after, "③ 真实 grades.db 未被触碰",
        (before[0], after[0]))

    print()
    print("=" * 70)
    print(f"  {PASS} 通过 / {FAIL} 失败")
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
