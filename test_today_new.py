"""「今日新获得」的回归测试（离线，用临时台账文件）。

背景 —— 这条为什么必须治根
--------------------------
用户反馈：化学「Unit 2 quiz」今天出分（A 96 / 100），但「新增成绩」里没有。

查下来**不是网络、也不是抓取**：缓存里那条成绩是全的（score / graded / percent），
问题在 `today_new._load()` 的一句话：

    if d.get("date") != today_str():
        return _empty()          # ← 跨天把基线整个丢掉

「新成绩」的判据于是变成「跟**今天第一次抓取**比」。
只要程序当天启动得比出分晚（关机、睡眠、下午才开），那条成绩就已经
在基线里了 → 永远不算新增。实测就是：今天 11:33 才启动，
11:33:58 建基线时那条成绩已经在页面上，于是报告 0 条。

正确的判据应该是「跟**上次观测到的状态**比」——
所以基线必须跨天持久，跨天只清空「今日报告」。

这个测试盯住的就是这件事。
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PASS = 0
FAIL = 0


def chk(ok: bool, label: str, extra=None) -> None:
    global PASS, FAIL
    print(f"  {'[OK]' if ok else '[X] '} {label}" + ("" if ok else f"   → {extra!r}"))
    if ok:
        PASS += 1
    else:
        FAIL += 1


def task(title: str, course: str = "Chemistry (AP Electives)", *,
         score: str = "", graded: bool = False, due: str = "2026-10-22",
         days_left: int = 0) -> dict:
    return {
        "title": title, "course": course, "url": f"u/{title}",
        "due_date": due, "days_left": days_left,
        "kind": "Homework", "category": "Summative",
        "score": score, "level": (score[:1] if score else ""),
        "percent": 96.0 if graded else None,
        "score_num": None, "score_max": None,
        "graded": graded, "submitted": False, "completed": graded,
        "has_attachment": False,
    }


def main() -> int:
    print("=" * 76)
    print("  今日新获得（跨天基线持久化）")
    print("=" * 76)

    from app import today_new as tn

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="mbpk_today_"))
    tn.TODAY_FILE = tmp / "today_new.json"      # 隔离到临时目录
    tn.reset()

    def update(tasks: list[dict]) -> dict:
        return tn.update({"tasks": tasks})

    def ledger() -> dict:
        return json.loads(tn.TODAY_FILE.read_text(encoding="utf-8"))

    def report() -> tuple[int, int]:
        d = tn.current()
        return len(d.get("grades") or []), len(d.get("tasks") or [])

    # ── ① 第一次抓取：只建基线，不报新增 ──
    print("\n── ① 首次运行：建立基线，不报新增 ──")
    quiz_ungraded = task("Unit 2 quiz")
    other = task("Unit 1 PES", score="A 100 / 100 pts", graded=True)
    d = update([quiz_ungraded, other])
    g, t = report()
    chk((g, t) == (0, 0), f"首轮不报新增（成绩 {g} / 作业 {t}）", (g, t))
    chk(len(d["base"]) == 2, "基线记了 2 条", len(d["base"]))
    key = tn._key(quiz_ungraded)
    chk(d["base"][key]["graded"] is False, "基线里这条**没有**成绩")

    # ── ② 同一天：没有变化 → 不报 ──
    print("\n── ② 同一天再抓一次（没变化）──")
    update([quiz_ungraded, other])
    g, t = report()
    chk((g, t) == (0, 0), "没有变化就不报", (g, t))

    # ── ③ 同一天：出分了 → 报「新成绩」──
    print("\n── ③ 同一天出分：应报 1 条新成绩 ──")
    quiz_graded = task("Unit 2 quiz", score="A 96 / 100 pts", graded=True)
    update([quiz_graded, other])
    g, t = report()
    chk((g, t) == (1, 0), f"1 条新成绩 / 0 条新作业（实际 {g}/{t}）", (g, t))
    got = (tn.current().get("grades") or [])[0]
    chk(got.get("score") == "A 96 / 100 pts", "分数正确", got.get("score"))

    # ── ④ 基线推进了：再抓一次不应重复报 ──
    print("\n── ④ 重复抓取：不应重复计一次 ──")
    update([quiz_graded, other])
    g, t = report()
    chk((g, t) == (1, 0), f"仍然只 1 条（实际 {g}/{t}）", (g, t))

    # ── ⑤ ★核心：跨天 + 出分发生在程序没开的时候 ──
    print("\n── ⑤ 跨天（本次 bug 的场景）──")
    tn.reset()
    #  第 1 天：作业还没出分
    day1 = [task("Unit 2 quiz"), other]
    update(day1)
    #  手动把它标成「昨天」，模拟跨天
    d = ledger()
    d["date"] = "2026-10-21"
    tn.TODAY_FILE.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")

    #  第 2 天（今天）：程序第一次启动时，这条**已经出分**了
    update([quiz_graded, other])
    g, t = report()
    chk((g, t) == (1, 0),
        f"跨天后仍应报出这条新成绩（实际 {g}/{t}）—— 这就是修好的点", (g, t))
    titles = [x.get("title") for x in (tn.current().get("grades") or [])]
    chk("Unit 2 quiz" in titles, f"报的正是 Unit 2 quiz（实际 {titles}）", titles)

    # ── ⑥ 跨天：跨天时报告清零，但基线沿用 ──
    print("\n── ⑥ 跨天：报告清零、基线沿用 ──")
    d = ledger()
    d["date"] = "2026-10-21"
    tn.TODAY_FILE.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    g, t = report()
    chk((g, t) == (0, 0), f"新的一天报告从空开始（实际 {g}/{t}）", (g, t))
    chk(len(ledger().get("base") or {}) >= 2, "基线还在（没被丢掉）",
        len(ledger().get("base") or {}))

    # ── ⑦ 新作业 → 后来出分：只归到成绩，不同时留在两处 ──
    print("\n── ⑦ 今天新布置的作业，随后出分 ──")
    tn.reset()
    update([other])                                  # 基线
    newhw = task("Unit 3 quiz", due="2026-10-30", days_left=8)
    update([other, newhw])                           # 新作业出现
    g, t = report()
    chk((g, t) == (0, 1), f"先报 1 条新作业（实际 {g}/{t}）", (g, t))
    newhw_graded = task("Unit 3 quiz", due="2026-10-30", days_left=8,
                        score="B 80 / 100 pts", graded=True)
    update([other, newhw_graded])                    # 出分
    g, t = report()
    chk((g, t) == (1, 0), f"出分后移到成绩里，作业那边清掉（实际 {g}/{t}）", (g, t))

    # ── ⑧ 改分：同一条要跟着更新 ──
    print("\n── ⑧ 老师改分：条目内容要更新 ──")
    changed = task("Unit 3 quiz", due="2026-10-30", days_left=8,
                   score="A 100 / 100 pts", graded=True)
    update([other, changed])
    got = (tn.current().get("grades") or [])[0]
    chk(got.get("score") == "A 100 / 100 pts", f"分数更新到最新（{got.get('score')}）",
        got.get("score"))

    # ── ⑨ 部分抓取：没抓到的课不应被抹掉基线 ──
    print("\n── ⑨ 部分抓取（某门课这次没抓到）──")
    tn.reset()
    a = task("A-quiz", course="Course A", score="A 90 / 100 pts", graded=True)
    b = task("B-quiz", course="Course B", score="B 80 / 100 pts", graded=True)
    update([a, b])
    base_before = len(ledger().get("base") or {})
    update([a])                                    # 只抓到 A，B 缺席
    base_after = len(ledger().get("base") or {})
    chk(base_after >= base_before,
        f"B 的基线没被抹掉（{base_before} → {base_after}）",
        (base_before, base_after))
    chk(tn._key(b) in (ledger().get("base") or {}), "B 仍在基线里")

    # ── 清理 ──
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("=" * 76)
    print(f"  {PASS} 通过 / {FAIL} 失败")
    print("=" * 76)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
