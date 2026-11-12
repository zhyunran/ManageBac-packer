"""成绩目标模拟（0.5.2）。

回答一个很实际的问题：**「这门课我想考到 X 分，剩下那些作业得拿多少？」**

────────────────────────────────────────────────
算法（照通行做法，不引入任何假想值）

  输入：课程的类别列表
        [{name, weight, percent, graded}]   —— 直接来自 ManageBac 页面
        + 用户填的目标分

  ① 已评分类别（graded=True 且有 percent）
       锁定的加权得分 held = Σ(percent × weight/100)
  ② 未评分类别
       可调整权重 adjustable = Σ(weight)          （未评分的那些）
  ③ 需要拿到多少
       required = (target - held) / (adjustable / 100)

  结论分四种：
    met          已锁定的分已经够目标了
    fixed        没有可调整的类别（全部已评分）且没达标
    unreachable  required > 100，当前假设下达不到
    possible     required ≤ 100，照这个分数就行
────────────────────────────────────────────────
"""
from __future__ import annotations

#  达标判定的容差（避免 79.999 与 80 被判成差一点）
EPS = 1e-6


def _is_graded(c: dict, pct: float | None) -> bool:
    """这个类别算「已出分」吗？

    ★ 以 ManageBac 页面给的 `graded` 为准（这正是文档承诺的语义）。
      以前这里只判 `percent is not None` —— 等于**完全忽略 graded**：
      万一出现「页面上已经有百分比、但该类别并未结束」的情形，
      就会被当成已锁定，然后告诉用户「这门课的成绩已经定了」，
      结论可能完全反了。

    ★ 兼容：`graded` 缺省时退回「有 percent 就算已出分」的老行为，
      这样老缓存 / 只传 name+weight+percent 的调用方不会突然算不出来。
    """
    if "graded" in c and c.get("graded") is not None:
        return bool(c.get("graded")) and pct is not None
    return pct is not None


def simulate(categories: list[dict] | None,
             target: float | None) -> dict:
    """算「要考到 target，剩下的类别得拿多少」。

    参数：
        categories —— [{name, weight, percent, graded}]
                      percent 为 None 表示还没出分
        target     —— 目标百分比（0-100）

    返回：
        {ok, status, held, adjustable, required, min, max,
         known, unknown, reason, formula}
    """
    cats = [c for c in (categories or []) if isinstance(c, dict)]

    # ── 校验 ──
    if target is None or not (0 <= float(target) <= 100):
        return {"ok": False, "reason": "目标分要在 0 到 100 之间"}

    rows: list[dict] = []
    for c in cats:
        try:
            w = float(c.get("weight"))
        except (TypeError, ValueError):
            continue
        if not (0 < w <= 100):
            continue
        pct = c.get("percent")
        try:
            pct = float(pct) if pct is not None else None
        except (TypeError, ValueError):
            pct = None
        if pct is not None and not (0 <= pct <= 100):
            pct = None
        rows.append({"name": str(c.get("name") or "未命名"),
                     "weight": w, "percent": pct,
                     "graded": _is_graded(c, pct)})

    if not rows:
        return {"ok": False,
                "reason": "这门课没有读到类别权重，算不了"
                          "（ManageBac 页面里得有 Task Category Averages）"}

    total_w = sum(r["weight"] for r in rows)
    #  权重必须接近 100。差得太多说明页面结构变了或权重没读全，
    #  这时**不猜**，直接说明算不了。
    if abs(total_w - 100) > 0.5:
        return {"ok": False,
                "reason": f"类别权重合计 {total_w:.1f}%，不是 100%，"
                          f"这次先不算（可能是页面还没显示全）"}

    known = [r for r in rows if r["graded"]]
    unknown = [r for r in rows if not r["graded"]]

    held = sum(r["percent"] * r["weight"] / 100.0 for r in known)
    adjustable = sum(r["weight"] for r in unknown)

    out = {
        "ok": True,
        "status": "",
        "held": round(held, 2),
        "adjustable": round(adjustable, 2),
        "required": None,
        "min": round(held, 2),
        "max": round(held + adjustable, 2),
        "known": [{"name": r["name"], "weight": r["weight"],
                   "percent": round(r["percent"], 2)} for r in known],
        "unknown": [{"name": r["name"], "weight": r["weight"]}
                    for r in unknown],        "reason": "",
        "formula": "",
        "target": round(float(target), 2),
    }

    # ── 分类讨论 ──
    if adjustable <= EPS:
        if held + EPS >= float(target):
            out["status"] = "met"
            out["reason"] = "所有类别都已出分，当前总分已经达到目标。"
        else:
            out["status"] = "fixed"
            out["reason"] = ("所有类别都已出分，没有可调整的部分 —— "
                             "这门课的成绩已经定了。")
        return out

    if held + EPS >= float(target):
        out["status"] = "met"
        out["reason"] = ("已出分的部分就够目标了，剩下的类别只要不拉低就行。")
        out["required"] = 0.0
        return out

    required = (float(target) - held) / (adjustable / 100.0)
    out["required"] = round(required, 2)

    if required > 100 + EPS:
        out["status"] = "unreachable"
        out["reason"] = (f"剩下的 {adjustable:.0f}% 就算全拿满分，"
                         f"总分也只能到 {held + adjustable:.2f}%。")
    else:
        out["status"] = "possible"
        out["reason"] = (f"剩下的类别合计占 {adjustable:.0f}%，"
                         f"加权平均要拿到 {required:.2f} 分。")

    #  公式写成能自己验算的样子（保留 4 位小数）
    out["formula"] = (f"已出分贡献 {held:.2f} + "
                      f"待出分类别均分 × {adjustable / 100:.4f} = 课程总评")
    return out
