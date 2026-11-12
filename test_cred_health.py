"""凭据体检的回归测试（离线，在临时目录里跑）。

对应的问题：credentials.json 读不出来时，`_load_credentials()` 静默返回 `{}`，
于是用户会**莫名其妙回到首次引导页** —— 看起来像「账号丢了」，程序一个字都不解释。

本测试锁住三条契约：
  ① 没有文件      → 凭据为空，但 health.ok = True（这是正常情况）
  ② 内容损坏      → 凭据为空，health.ok = False 且写明原因
  ③ 内容为空对象  → 同样算不健康（存在但没内容）
  ④ 正常文件      → 读到内容，health.ok = True
  ⑤ 坏文件**不会被删掉**（里面的账号可能还能人工救回来）
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402

PASS = 0
FAIL = 0


def chk(ok: bool, label: str, extra=None) -> None:
    global PASS, FAIL
    print(f"  {'[OK]' if ok else '[X] '} {label}" + ("" if ok else f"   → {extra!r}"))
    if ok:
        PASS += 1
    else:
        FAIL += 1


def main() -> int:
    print("=" * 70)
    print("  凭据体检契约")
    print("=" * 70)

    real_root, real_res = config.ROOT, config.RES_DIR
    td = pathlib.Path(tempfile.mkdtemp(prefix="mbpk_cred_"))
    try:
        config.ROOT = td
        config.RES_DIR = td / "res"
        cred = td / "credentials.json"

        def reset() -> None:
            config.CRED_HEALTH = {"ok": True, "note": "", "path": ""}

        # ① 没有文件
        print("\n── ① 没有凭据文件（正常情况）──")
        reset()
        d = config._load_credentials()
        chk(d == {}, "返回空 dict", d)
        chk(config.CRED_HEALTH["ok"] is True,
            "health.ok = True（没填过 ≠ 坏了）", config.CRED_HEALTH)

        # ② 损坏
        print("\n── ② 文件损坏 ──")
        cred.write_text("{ this is not json", encoding="utf-8")
        reset()
        d = config._load_credentials()
        chk(d == {}, "返回空 dict", d)
        chk(config.CRED_HEALTH["ok"] is False, "health.ok = False", config.CRED_HEALTH)
        chk("读不出来" in config.CRED_HEALTH.get("note", ""),
            "原因写明「读不出来」", config.CRED_HEALTH.get("note"))
        chk(cred.exists(), "坏文件没有被删掉（可人工救回）")

        # ③ 空对象
        print("\n── ③ 文件存在但内容为空 ──")
        cred.write_text("{}", encoding="utf-8")
        reset()
        config._load_credentials()
        chk(config.CRED_HEALTH["ok"] is False, "health.ok = False", config.CRED_HEALTH)
        chk("不完整" in config.CRED_HEALTH.get("note", ""),
            "原因写明「内容不完整」", config.CRED_HEALTH.get("note"))

        # ④ 正常
        print("\n── ④ 正常凭据 ──")
        payload = {"base_url": "https://example.invalid", "username": "u",
                   "password": "p"}
        cred.write_text(json.dumps(payload), encoding="utf-8")
        reset()
        d = config._load_credentials()
        chk(d == payload, "读回完整内容", list(d.keys()))
        chk(config.CRED_HEALTH["ok"] is True, "health.ok = True", config.CRED_HEALTH)
    finally:
        config.ROOT, config.RES_DIR = real_root, real_res
        config.CRED_HEALTH = {"ok": True, "note": "", "path": ""}
        import shutil
        shutil.rmtree(td, ignore_errors=True)

    print()
    print("=" * 70)
    print(f"  {PASS} 通过 / {FAIL} 失败")
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
