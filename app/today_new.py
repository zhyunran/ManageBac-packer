"""「今日新获得」—— 记录今天新出的成绩和作业。

【用户要求】
    「新增今日新获得成绩作业项目，紧随代办作业之后」
    → 成绩页在「全部待办」下面加一个区块，列出**今天新出现**的
      成绩（新出分）和作业（新布置）。

【为什么不直接用晚报的 diff】
    晚报的 diff 是「跟**上一次快照**比」（`digest.save_snapshot`），
    而快照每次抓取都被覆盖。所以它只能回答
    「这次抓取比上次多了什么」，不能回答「**今天**新增了什么」。

    举例：早 8 点抓了一次（快照 A），中午出了一条新作业，
    下午 2 点再抓（快照 B）——
    此时 diff(A,B) 有东西，但 diff 只保留到 2 点这一次；
    晚上 8 点又抓一次（快照 C），diff(B,C) 就**没有**中午那条了。
    用户晚上打开组件，看到的「今日新增」是空的 —— 显然不对。

    → 所以要有一份**按日期累积**的台账：

        data/today_new.json
        {
          "date": "2026-09-23",         # 台账是哪一天的
          "grades": {"<key>": {...}},   # 今天新出分的（按 key 去重）
          "tasks":  {"<key>": {...}}    # 今天新布置的
        }

    每天第一次抓取时（date 不是今天）把台账清空重来。
    当天内每次抓取只**往里加**，不覆盖 —— 这样中午那条
    一直留到当天结束。

【跟谁比「新」】
    跟**当天第一次抓取时**的全量快照比。
    那份快照也存进同一个文件（`"base"`），当天不再更新。
    这样「今天新增」= 当前数据 - 今天开局的数据，语义最直观。

【去重 key】
    和 digest 保持一致：优先 core_tasks 的 id，
    退回「课程|标题」。两条路独立去重（成绩和作业各一份）。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import date
from pathlib import Path

from . import config

log = logging.getLogger(__name__)

#  台账文件
TODAY_FILE = config.DATA_DIR / "today_new.json"

#  同时最多留多少条（防止异常数据把界面撑爆）
MAX_ITEMS = 60
#  基线（跨天持久的「上次观测状态」）的规模上限。
#  正常一学期几百条任务，超了说明积累了很久的老键。
MAX_BASE = 400
MAX_BASE_STALE = 80

#  用于「新的一天」只记一次日志
_RESET_LOGGED = ""

_lock = threading.RLock()


# ══════════════════════════════════════════════════════════
#  工具
# ══════════════════════════════════════════════════════════

def today_str() -> str:
    return date.today().isoformat()


def _key(t: dict) -> str:
    """任务的稳定标识。与 digest._task_key 保持同一套规则。"""
    tid = t.get("id") or t.get("tid")
    if tid:
        return f"tid:{tid}"
    return f"t:{(t.get('course') or '').strip()}|" \
           f"{(t.get('title') or '').strip()}"


def _slim(t: dict) -> dict:
    """只留下界面要用的字段 —— 台账越小越好读。"""
    return {
        "title": t.get("title", ""),
        "course": t.get("course", ""),
        "url": t.get("url", ""),
        "due_date": t.get("due_date", ""),
        "days_left": t.get("days_left"),
        "kind": t.get("kind", ""),
        "category": t.get("category", ""),
        "score": t.get("score", ""),
        "level": t.get("level", ""),
        "percent": t.get("percent"),
        "score_num": t.get("score_num"),
        "score_max": t.get("score_max"),
        "graded": bool(t.get("graded")),
        "submitted": bool(t.get("submitted")),
        "completed": bool(t.get("completed")),
        "has_attachment": bool(t.get("has_attachment")),
    }


#  「没有分数」的占位文本 —— 名单与 scraper._apply_grade 保持一致。
#  ★ 这些字符串会被写进 score 字段，但语义是「还没评分」，
#    不能因为字段非空就当成有成绩（实测踩过：
#    未评分的作业混进了「今日新成绩」列表，分数栏显示 Not Assessed Yet）。
_PLACEHOLDER_SCORE = (
    "not assessed", "not graded", "ungraded",
    "not submitted", "n/a", "待评分", "未评分",
)
_DASH_SCORE = ("-", "—", "–")


def _is_graded(t: dict) -> bool:
    """这条任务算不算「有成绩」。

    ★ 判据和 digest 一致：`graded` 为真**或** `score` 是**真分数**。
      两个都要看 —— 有的任务给了分数文字但 graded 还是 false。

    ★★ 但 `score` 非空 ≠ 有成绩 ★★
      "Not Assessed Yet" 这类占位文本也会被写进 score。
      不排除的话，未评分的作业会混进「今日新成绩」。
    """
    if t.get("graded"):
        return True
    s = (t.get("score") or "").strip()
    if not s or s in _DASH_SCORE:
        return False
    low = s.lower()
    if any(k in low for k in _PLACEHOLDER_SCORE):
        return False
    return True


def _grade_val(t: dict):
    """取「能代表成绩的那几个字段」。

    ★ 只比成绩，不比 days_left / completed 这些每天都在变的字段 ——
      拿它们判断会让台账每次抓取都被改写（等于没缓存）。
    """
    return (
        (t.get("score") or "").strip(),
        (t.get("level") or "").strip(),
        t.get("percent"),
    )


def _grade_changed(old: dict, new: dict) -> bool:
    """台账里记的那条成绩，跟现在读到的**不一样**了吗。

    老师登了成绩又改分时，这里会返回 True，调用方就更新台账。
    """
    try:
        a = _grade_val(old or {})
        b = _grade_val(new or {})
        #  percent 是浮点，容忍极小误差（避免 87.5 vs 87.500001 这种假变化）
        if a[0] != b[0] or a[1] != b[1]:
            return True
        pa, pb = a[2], b[2]
        if pa is None and pb is None:
            return False
        if pa is None or pb is None:
            return True
        try:
            return abs(float(pa) - float(pb)) > 0.01
        except (TypeError, ValueError):
            return pa != pb
    except Exception:
        return False


# ══════════════════════════════════════════════════════════
#  读 / 写
# ══════════════════════════════════════════════════════════

def _empty() -> dict:
    return {"date": today_str(), "base": {}, "grades": {}, "tasks": {}}


def _load() -> dict:
    """读台账。

    ★★ 跨天时只清空「今日报告」，**保留基线**。★★

      这曾经是个真 bug：以前 `date != today` 就 `return _empty()`，
      等于**每天把基线丢掉重建**。于是「新成绩」的判据变成了
      「跟今天第一次抓取比」——

          今天 12:00 才开机 → 11:00 出的成绩已经在基线里
          → 永远不算「今日新增」，界面上一条都看不到

      实测踩到：化学「Unit 2 quiz」今天出分（A 96/100），
      但程序 11:33 才第一次启动，那条成绩直接被吸进基线，报告 0 条。

      改成：基线是**跨天持久的**「上次观测到的状态」，
      跨天只把当天的报告清空。判据从「跟今天比」变成「跟上次看的时候比」，
      才是「新增」这两个字的本意。
    """
    try:
        if not TODAY_FILE.exists():
            return _empty()
        d = json.loads(TODAY_FILE.read_text(encoding="utf-8"))
        if not isinstance(d, dict):
            return _empty()

        out = _empty()
        base = d.get("base")
        out["base"] = base if isinstance(base, dict) else {}

        if d.get("date") == today_str():
            for k in ("grades", "tasks"):
                v = d.get(k)
                out[k] = v if isinstance(v, dict) else {}
            out["date"] = d["date"]
        else:
            #  新的一天：报告清零，基线留着继续当参照
            out["grades"], out["tasks"] = {}, {}
            #  ★ 这条日志每天只该出现一次 ★
            #    _load() 会被「界面每秒轮询」和「每轮抓取」反复调用，
            #    而清零要等下一次 update() 才落盘 ——
            #    于是同一天里会反复打这句话（实测 1173 次）。
            global _RESET_LOGGED
            if _RESET_LOGGED != str(d.get("date") or ""):
                _RESET_LOGGED = str(d.get("date") or "")
                log.info("今日新增：新的一天，报告已清零（基线沿用 %d 条）",
                         len(out["base"]))
        return out
    except Exception as e:
        log.debug("读今日新增台账失败（用空的）：%s", e)
        return _empty()


def _save(d: dict) -> None:
    try:
        config.ensure_dirs()
        tmp = TODAY_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(TODAY_FILE)
    except Exception as e:
        log.debug("存今日新增台账失败：%s", e)


# ══════════════════════════════════════════════════════════
#  主流程：每次抓取完成后调一次
# ══════════════════════════════════════════════════════════

def update(data: dict) -> dict:
    """拿本轮抓取的数据更新台账，返回更新后的内容。

    ★ 为什么要在**抓取完成后**调，而不是界面每次轮询时算
      界面轮询是每秒一次（为了倒计时），拿它做 diff 太频繁；
      而且 diff 需要「上一轮」这个概念，只有抓取完成才成立。
    """
    with _lock:
        tasks = [t for t in (data.get("tasks") or []) if isinstance(t, dict)]
        cur = {_key(t): t for t in tasks}

        d = _load()

        #  今天第一次抓取 → 把当前全量记成基线，台账清空
        if not d["base"]:
            d["base"] = {k: _slim(t) for k, t in cur.items()}
            d["grades"], d["tasks"] = {}, {}
            _save(d)
            log.info("今日新增：建立今日基线（%d 条任务）", len(cur))
            return d

        base = d["base"]
        n_g = n_t = 0
        n_g_upd = 0

        for k, t in cur.items():
            old = base.get(k)

            #  ① 今天新出分的 —— 只有现在有成绩、基线里没有
            if _is_graded(t):
                had = bool(old and _is_graded(old))
                if not had:
                    #  ★ 它可能**先前**作为「新作业」记在 tasks 里
                    #    （布置时还没出分，后来才批改）。
                    #    现在出分了就该归到「新成绩」，不能两边都留 ——
                    #    否则界面上同一条任务会出现两次。
                    if d["tasks"].pop(k, None) is not None:
                        n_t = max(0, n_t - 1)

                    if k not in d["grades"]:
                        d["grades"][k] = _slim(t)
                        n_g += 1
                    else:
                        #  ★★ 已经有条目了 —— 但**分数可能又被改过**。
                        #
                        #  【这个 bug 长什么样】
                        #    老师先给了一个分（比如 F 0 / 100），台账记下；
                        #    老师随后改成 A 100 / 100 —— 原来的代码
                        #    因为「k 已在 grades 里」就什么都不做，
                        #    于是界面上永远显示第一次那个分。
                        #    用户的反馈原话：
                        #      「对于『老师登成绩然后又改了成绩』不敏感，
                        #        一直保持原成绩」
                        #
                        #  【修法】
                        #    只要**现在读到的成绩和台账里的不一样**，
                        #    就把台账那一条更新成最新值。
                        #    条目本身留着（它确实还是「今天新出的成绩」），
                        #    只是内容跟着页面走。
                        #
                        #  ★ 只比成绩字段，不比整个对象 ——
                        #    days_left、completed 这些每天都会变，
                        #    拿它们判断会导致「每次抓取都在刷台账」。
                        if _grade_changed(d["grades"][k], t):
                            d["grades"][k] = _slim(t)
                            n_g_upd += 1
                else:
                    #  ② 基线里**早就有成绩**了 —— 但老师可能改过分。
                    #
                    #  ★ 这一段是因为基线改成「跨天持久 + 随观测推进」
                    #    之后必须补上的：基线不再滞后，所以「改分」不会再
                    #    走进上面 `if not had` 那条路。如果这里不处理，
                    #    老师把 B 80 改成 A 100 就永远显示旧分
                    #    （用户之前专门反馈过这个问题）。
                    #
                    #    判据：基线里的旧值 ≠ 现在读到的值 → 算「成绩更新」，
                    #    并把它放进今天的报告里（不管它原本是哪天的成绩，
                    #    「今天被改动」本身就值得看）。
                    if _grade_changed(old, t):
                        d["grades"][k] = _slim(t)
                        n_g_upd += 1
                continue        #  有成绩的就不再算「新作业」

            #  ② 今天新布置的 —— 基线里完全没有这条
            #  ★ 注意：这里**不要**再写 elif 去处理「后来出分」的情形 ——
            #    那种情况上面 `if _is_graded(t)` 已经接走了，
            #    写在下面会变成永远走不到的死代码（我第一版就写错了）。
            if old is None and k not in d["tasks"]:
                d["tasks"][k] = _slim(t)
                n_t += 1

        #  ★ 把基线推进到「刚观测到的状态」。
        #    否则同一次变化会在之后每次抓取里被反复报告（每天都算「新增」）。
        #    只合并**本次真的看到**的键 —— 一次部分失败（某些课程没抓到）
        #    不能把那些课的基线抹掉，否则下一轮会把它们的成绩全当成新的。
        for k, t in cur.items():
            d["base"][k] = _slim(t)

        #  基线不能无限长：课程/任务换掉以后老键再也没人用。
        #  超限就只留「本次还看得到的」+ 少量过时的（防止同一任务改名后
        #  重新变成「新作业」）。
        if len(d["base"]) > MAX_BASE:
            keep_now = {k: v for k, v in d["base"].items() if k in cur}
            stale = {k: v for k, v in d["base"].items() if k not in cur}
            d["base"] = {**dict(list(stale.items())[-MAX_BASE_STALE:]),
                         **keep_now}

        #  上限保护：超了就把最早的丢掉（dict 保持插入顺序）
        for key in ("grades", "tasks"):
            if len(d[key]) > MAX_ITEMS:
                keep = list(d[key].items())[-MAX_ITEMS:]
                d[key] = dict(keep)

        _save(d)
        if n_g or n_t or n_g_upd:
            log.info("今日新增：%d 条新成绩 / %d 条新作业 / %d 条成绩更新",
                     n_g, n_t, n_g_upd)
        return d


def drop_recalled(pairs) -> int:
    """把「已被学校收回的成绩」从今日新增台账里撤掉。

    ★ 为什么需要：
      老师先把成绩登出来、程序记进「今日新成绩」，
      随后老师把成绩收回 —— 这条作业从成绩册消失了。
      台账如果不动，界面上会一直挂着一条**已经不存在的成绩**，
      而且用户点进去也看不到（页面上已经没有了），只会觉得程序在乱报。
    """
    if not pairs:
        return 0
    with _lock:
        d = _load()
        gs = d.get("grades") or {}
        n = 0
        for course, title in pairs:
            k = f"t:{(course or '').strip()}|{(title or '').strip()}"
            if k in gs:
                gs.pop(k, None)
                n += 1
        if n:
            d["grades"] = gs
            _save(d)
            log.info("今日新增：撤下 %d 条已被收回的成绩", n)
        return n


def current() -> dict:
    """给界面读的「今天新增」（不重算，只读台账）。

    返回结构：
        {
          ok, date,
          grades: [ {...}, ... ],   # 新出分的
          tasks:  [ {...}, ... ],   # 新布置的
          n_grades, n_tasks
        }

    ★ 顺序：成绩按 course 排（同科目的放一起好看），
      作业按 days_left 排（急的在前）。
    """
    with _lock:
        d = _load()
        grades = list(d["grades"].values())
        tasks = list(d["tasks"].values())

    grades.sort(key=lambda x: (x.get("course") or "", x.get("title") or ""))
    tasks.sort(key=lambda x: (x.get("days_left") is None,
                              x.get("days_left") or 0))
    return {
        "ok": True,
        "date": d.get("date") or today_str(),
        "grades": grades,
        "tasks": tasks,
        "n_grades": len(grades),
        "n_tasks": len(tasks),
    }


def reset() -> dict:
    """清掉台账（设置里「重来」或排查问题时用）。"""
    with _lock:
        d = _empty()
        _save(d)
        return {"ok": True}


# ══════════════════════════════════════════════════════════
#  自测
# ══════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    print("=" * 62)
    print("  today_new 自测")
    print("=" * 62)

    bak = TODAY_FILE.read_bytes() if TODAY_FILE.exists() else None
    try:
        reset()

        print("\n① 今天第一次抓取：只建基线，不该有新增")
        t1 = [
            {"id": 1, "title": "旧作业", "course": "数学"},
            {"id": 2, "title": "已出分", "course": "物理",
             "score": "A 90 / 100 pts", "graded": True},
        ]
        update({"tasks": t1})
        c = current()
        print(f"   新增：{c['n_grades']} 成绩 / {c['n_tasks']} 作业"
              f"   （应该 0 / 0）")
        assert c["n_grades"] == 0 and c["n_tasks"] == 0

        print("\n② 新出一条作业 + 一条新成绩")
        t2 = t1 + [
            {"id": 3, "title": "新布置的", "course": "化学",
             "due_date": "2026-09-25", "days_left": 2},
            {"id": 4, "title": "新出分", "course": "英语",
             "score": "B+ 88 / 100 pts", "graded": True},
        ]
        #  注意：id=2 那条基线里就有成绩，不该被算成「新出分」
        update({"tasks": t2})
        c = current()
        print(f"   新增：{c['n_grades']} 成绩 / {c['n_tasks']} 作业"
              f"   （应该 1 / 1）")
        print(f"   成绩：{[g['title'] for g in c['grades']]}")
        print(f"   作业：{[t['title'] for t in c['tasks']]}")
        assert c["n_grades"] == 1, c["n_grades"]
        assert c["n_tasks"] == 1, c["n_tasks"]
        assert c["grades"][0]["title"] == "新出分"
        assert c["tasks"][0]["title"] == "新布置的"

        print("\n③ 再抓一次（数据没变）—— 不该重复累加")
        update({"tasks": t2})
        c = current()
        print(f"   新增：{c['n_grades']} 成绩 / {c['n_tasks']} 作业"
              f"   （还是 1 / 1）")
        assert c["n_grades"] == 1 and c["n_tasks"] == 1

        print("\n④ 中午那条作业，晚上再抓还在（当天累积）")
        t3 = t2 + [{"id": 5, "title": "下午又加一条", "course": "生物",
                    "days_left": 5}]
        update({"tasks": t3})
        c = current()
        names = [t["title"] for t in c["tasks"]]
        print(f"   作业列表：{names}")
        assert "新布置的" in names and "下午又加一条" in names

        print("\n⑤ 任务清单变大（基线不认识的新条目）按新增算")
        assert c["n_tasks"] == 2, c["n_tasks"]

        print("\n[OK] 全部通过")
    finally:
        if bak is not None:
            TODAY_FILE.write_bytes(bak)
        elif TODAY_FILE.exists():
            TODAY_FILE.unlink()
