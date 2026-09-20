"""数据备份 —— 导出 / 导入（借鉴 CampusDesk 的做法）。

【为什么要做】

  现在这些**只在本机**的东西一旦丢了就没了：
      · 「我已完成，不再提示」的手动标记
      · 晚报历史
      · 成绩历史（SQLite）

  换电脑、重装、手滑删了 data 目录 → 全部要重来。

【备份什么 / 不备份什么】（ 这条最重要）

  [OK] **备份**：只属于你的、无法重新抓到的数据
       · manual_done   —— 你手动标记的完成
       · digests       —— 晚报历史（每天的快照 diff）
       · grades_db     —— 成绩历史（SQLite 里的时间序列）
       · settings      —— 刷新间隔、时区等偏好

  [X] **不备份**：能重新抓的、或者**敏感的**
       · credentials.json  ——  账号密码（绝不进备份包）
       · session_cookies   ——  登录凭据（等于钥匙）
       · schedule_token    ——  课表 JWT
       · cache / task_detail / attach_probe —— 重抓就有

  CampusDesk 的 README 里明确写着：
  > 「导入/导出 JSON；包含学习数据，**不包含网站 Cookie 或密码**。」
  这个原则必须遵守 —— 否则备份文件一旦泄漏就是账号泄漏。

【安全设计】（参考 CampusDesk 的 readSavedState）

  ① 导入前校验大小上限（防超大文件把内存吃光）
  ② 校验 `version` 字段（防导入别的软件的 JSON）
  ③ 校验每个字段的类型和范围（防脏数据污染）
  ④ 导入前**自动备份当前数据**（可以反悔）
"""
from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from . import config

log = logging.getLogger("backup")

# 备份格式版本（将来结构变化时用它做迁移）
BACKUP_VERSION = 1

# 备份文件大小上限（20 MB —— 与 CampusDesk 一致）
MAX_BYTES = 20_000_000

# 导出的文件名前缀
PREFIX = "ManageBac-备份"


def _out_dir() -> Path:
    """导出到哪里 —— 桌面（用户最容易找到）。"""
    import os
    for cand in (Path(os.environ.get("USERPROFILE", "")) / "Desktop",
        Path(os.environ.get("USERPROFILE", "")) / "OneDrive" / "Desktop",
    ):
        if cand.exists():
            return cand
    return config.ROOT


# ---------------------------------------------------------------- 读取

def _read_manual_marks() -> dict:
    try:
        from . import manual_done
        return manual_done.all_marks()
    except Exception as e:
        log.warning("读取手动标记失败：%s", e)
        return {}


def _read_digests() -> list:
    try:
        from . import digest
        return digest.list_all(60)
    except Exception as e:
        log.warning("读取晚报失败：%s", e)
        return []


def _read_grade_history(limit: int = 2000) -> list[dict]:
    """从 SQLite 里读出成绩历史。

     只读，不做任何修改；出错就返回空（备份不因为一部分失败而全废）。
    """
    try:
        db = config.DB_FILE
        if not db.exists():
            return []
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            conn.row_factory = sqlite3.Row
            cur = conn.execute("SELECT seen_at, course, name, score, max_score, percent, "
                "gpa, category, due_date, submitted, overdue, url "
                "FROM grades ORDER BY seen_at DESC LIMIT ?", (limit,)
            )
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()
    except Exception as e:
        log.warning("读取成绩历史失败：%s", e)
        return []


def _read_settings() -> dict:
    """可携带的偏好设置（不含任何凭据）。"""
    return {
        "auto_refresh_sec": config.AUTO_REFRESH_SEC,
        "auto_refresh_text": config.AUTO_REFRESH_TEXT,
        "fetch_mode": "http",
    }


# ---------------------------------------------------------------- 导出

def export(path: str | Path | None = None) -> dict:
    """导出备份。返回 {ok, path, counts, bytes} 或 {ok:False, error}。"""
    try:
        config.ensure_dirs()

        payload = {
            "app": "campus-pulse",
            "version": BACKUP_VERSION,
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "note": ("本文件只包含学习数据（手动标记 / 晚报 / 成绩历史），"
                     "**不包含**账号密码或登录凭据。"),
            "data": {
                "manual_done": _read_manual_marks(),
                "digests": _read_digests(),
                "grade_history": _read_grade_history(),
                "settings": _read_settings(),
            },
        }

        if path is None:
            stamp = datetime.now().strftime("%Y%m%d-%H%M")
            path = _out_dir() / f"{PREFIX}-{stamp}.json"
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        # 原子写
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(path)

        size = path.stat().st_size
        counts = {
            "manual_done": len(payload["data"]["manual_done"]),
            "digests": len(payload["data"]["digests"]),
            "grade_history": len(payload["data"]["grade_history"]),
        }
        log.info("已导出备份：%s（%s 字节，%s）", path, size, counts)
        return {"ok": True, "path": str(path), "counts": counts, "bytes": size}

    except Exception as e:
        log.warning("导出备份失败：%s", e)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------- 导入

class BadBackup(Exception):
    """备份文件不合法。"""


def validate(payload) -> dict:
    """校验备份结构。不合法就抛 BadBackup（ 宁可拒绝也不导入脏数据）。

    参考 CampusDesk 的 readSavedState：先看 version，再逐字段验。
    """
    if not isinstance(payload, dict):
        raise BadBackup("文件内容不是 JSON 对象")
    if payload.get("app") != "campus-pulse":
        raise BadBackup("这不是本软件的备份文件")
    if payload.get("version") != BACKUP_VERSION:
        raise BadBackup(f"备份版本不匹配（文件 {payload.get('version')}，"
            f"本程序 {BACKUP_VERSION}）"
        )
    data = payload.get("data")
    if not isinstance(data, dict):
        raise BadBackup("缺少 data 字段")

    # ── 逐字段校验（类型 + 范围）──
    marks = data.get("manual_done", {})
    if not isinstance(marks, dict):
        raise BadBackup("manual_done 必须是对象")
    if len(marks) > 10000:
        raise BadBackup("manual_done 条目过多")
    for k, v in list(marks.items())[:50]:
        if not isinstance(k, str) or len(k) > 300:
            raise BadBackup("manual_done 的键格式不对")
        if not isinstance(v, (bool, int, str, dict)):
            raise BadBackup("manual_done 的值格式不对")

    digests = data.get("digests", [])
    if not isinstance(digests, list):
        raise BadBackup("digests 必须是数组")
    if len(digests) > 400:
        raise BadBackup("digests 条目过多")

    hist = data.get("grade_history", [])
    if not isinstance(hist, list):
        raise BadBackup("grade_history 必须是数组")
    if len(hist) > 50000:
        raise BadBackup("grade_history 条目过多")

    settings = data.get("settings", {})
    if not isinstance(settings, dict):
        raise BadBackup("settings 必须是对象")

    return data


def _backup_current() -> Path | None:
    """导入前先把当前数据复制一份（可以反悔）。"""
    try:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dst = config.DATA_DIR / f"_before_import_{stamp}"
        dst.mkdir(parents=True, exist_ok=True)
        n = 0
        for name in ("manual_done.json", "digests.json", "digest_snapshot.json"):
            src = config.DATA_DIR / name
            if src.exists():
                shutil.copy2(src, dst / name)
                n += 1
        db = config.DB_FILE
        if db.exists():
            shutil.copy2(db, dst / db.name)
            n += 1
        log.info("导入前已备份当前数据到 %s（%d 个文件）", dst, n)
        return dst
    except Exception as e:
        log.warning("导入前备份失败：%s", e)
        return None


def _write_manual_marks(marks: dict) -> int:
    try:
        from . import manual_done
        with manual_done._lock:              # noqa: SLF001（同包内使用）
            manual_done.FILE.write_text(json.dumps(marks, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
        return len(marks)
    except Exception as e:
        log.warning("写入手动标记失败：%s", e)
        return 0


def _write_digests(items: list) -> int:
    try:
        from . import digest
        digest._save_all(list(items))        # noqa: SLF001
        return len(items)
    except Exception as e:
        log.warning("写入晚报失败：%s", e)
        return 0


def import_from(path: str | Path, merge: bool = True) -> dict:
    """导入备份。

    Args:
        merge: True → 与现有数据**合并**（不丢现有记录，更安全）
               False → 完全替换
    """
    try:
        p = Path(path)
        if not p.exists():
            return {"ok": False, "error": "文件不存在"}

        # ① 大小上限（先看大小，避免把大文件读进内存）
        size = p.stat().st_size
        if size > MAX_BYTES:
            return {"ok": False,
                    "error": f"文件太大（{size / 1e6:.1f} MB > "
                             f"{MAX_BYTES / 1e6:.0f} MB）"}

        # ② 解析
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            return {"ok": False, "error": f"不是有效的 JSON：{e}"}

        # ③ 严格校验
        try:
            data = validate(payload)
        except BadBackup as e:
            return {"ok": False, "error": str(e)}

        # ④ 先备份当前数据（可反悔）
        before = _backup_current()

        # ⑤ 写入
        n_marks = 0
        if "manual_done" in data:
            src_marks = data["manual_done"] or {}
            if merge:
                cur = _read_manual_marks()
                merged = dict(cur)
                merged.update(src_marks)        # 导入的优先
                n_marks = _write_manual_marks(merged)
            else:
                n_marks = _write_manual_marks(src_marks)

        n_digests = 0
        if data.get("digests"):
            if merge:
                cur = _read_digests()
                by_date = {x.get("date"): x for x in cur if x.get("date")}
                for x in data["digests"]:
                    if isinstance(x, dict) and x.get("date"):
                        by_date[x["date"]] = x
                n_digests = _write_digests(list(by_date.values()))
            else:
                n_digests = _write_digests(data["digests"])

        result = {
            "ok": True,
            "manual_done": n_marks,
            "digests": n_digests,
            "grade_history": len(data.get("grade_history") or []),
            "grade_history_note": ("成绩历史只做备份留档，不写回数据库"
                "（避免与当前抓取结果冲突）"
            ),
            "backup_before": str(before) if before else None,
            "merged": merge,
        }
        log.info("已导入备份：%s", result)
        return result

    except Exception as e:
        log.warning("导入备份失败：%s", e)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------- 状态

def status() -> dict:
    """给界面用：当前有多少可备份的数据。"""
    return {
        "ok": True,
        "manual_done": len(_read_manual_marks()),
        "digests": len(_read_digests()),
        "export_dir": str(_out_dir()),
        "max_mb": MAX_BYTES / 1e6,
        "version": BACKUP_VERSION,
    }


# ---------------------------------------------------------------- 自测

def _self_test() -> int:
    """验证导出→校验→导入的完整链路，以及安全要求。"""
    print("=" * 68)
    print("  备份模块自测")
    print("=" * 68)
    fails = 0

    # ① 导出
    print("\n[1] 导出")
    r = export()
    if not r.get("ok"):
        print(f"    [X] 导出失败：{r.get('error')}")
        return 1
    p = Path(r["path"])
    print(f"    [OK] {p.name}  {r['bytes']:,} 字节")
    print(f"         内容：{r['counts']}")

    # ②  安全检查：不能含凭据
    print("\n[2]  安全检查（备份里绝不能有凭据）")
    txt = p.read_text(encoding="utf-8")
    bad = {
        "密码字段": ("password", '"pw"', "zongheng"),
        "Cookie": ("_managebac_session", "cookies", "session_cookies"),
        "课表 token": ("access_token", "schedule_token", "eyJ"),
        "邮箱": ("@", ""),          # 只检查 @ 符号，不写具体域名
        "手机号": ("13800000000",),
    }
    for name, keys in bad.items():
        hits = [k for k in keys if k in txt]
        if hits:
            fails += 1
            print(f"    [X] 发现「{name}」：{hits}")
        else:
            print(f"    [OK] 无「{name}」")

    # ③ 校验器：正常文件应该通过
    print("\n[3] 校验器（正常备份应通过）")
    try:
        data = validate(json.loads(txt))
        print(f"    [OK] 通过，字段：{sorted(data.keys())}")
    except BadBackup as e:
        fails += 1
        print(f"    [X] 意外拒绝：{e}")

    # ④ 校验器：坏文件应该被拒绝
    print("\n[4] 校验器（坏文件应被拒绝）")
    bads = [
        ({}, "空对象"),
        ({"app": "other", "version": 1, "data": {}}, "别的软件"),
        ({"app": "campus-pulse", "version": 99, "data": {}}, "版本不符"),
        ({"app": "campus-pulse", "version": 1}, "缺 data"),
        ({"app": "campus-pulse", "version": 1, "data": {"manual_done": []}},
         "manual_done 类型错"),
        ({"app": "campus-pulse", "version": 1, "data": {"digests": {}}},
         "digests 类型错"),
    ]
    for payload, note in bads:
        try:
            validate(payload)
            fails += 1
            print(f"    [X] 应该拒绝但通过了：{note}")
        except BadBackup:
            print(f"    [OK] 已拒绝：{note}")

    # ⑤ 导入（合并模式）
    print("\n[5] 导入（合并模式）")
    r2 = import_from(p, merge=True)
    if r2.get("ok"):
        print(f"    [OK] 手动标记 {r2['manual_done']} 条 / "
              f"晚报 {r2['digests']} 期")
        if r2.get("backup_before"):
            print(f"         导入前已自动备份 → "
                  f"{Path(r2['backup_before']).name}")
    else:
        fails += 1
        print(f"    [X] 导入失败：{r2.get('error')}")

    # 清理
    try:
        p.unlink()
    except Exception:
        pass

    print()
    print("=" * 68)
    if fails:
        print(f"  [X] {fails} 项不合格")
    else:
        print("  [OK] 全部通过（含安全检查）")
    print("=" * 68)
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    import sys
    sys.exit(_self_test())
