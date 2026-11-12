"""成绩解析与 GPA 换算。

换算规则全部集中在本文件的常量里，按你学校的规定改这里即可。

────────────────────────────────────────────────
本版（0.5.1）按「绝大多数学校的公共口径」重编，依据：

  ① 逐门换算、再平均 —— 每门课先转成 grade points，
     然后按学分加权平均（等学分 = 简单平均）。
     **不能**先把百分比平均完再换算一次（会系统性偏高/偏低）。

  ② 字母点数表（标准 4.0 制，美高/大学最通行）：
     A=4.0  A-=3.7  B+=3.3  B=3.0  B-=2.7
     C+=2.3  C=2.0  C-=1.7  D+=1.3  D=1.0  D-=0.7  F=0.0

  ③ 百分制切段（0.5.1 按公开的未加权 4.0 标准表校准，
     与美高/大学最通行版本逐档一致）：
     93/90/87/83/80/77/73/70/67/65 依次对应上表各档；
     65 以下 = F(0.0)。（旧版 63 起算 D、60 起算 D- 偏宽，
     标准表里 D 档是 65-66，再低就是 F。）

  ④ 加权 GPA：Honors/荣誉 +0.5，AP/IB(HL) +1.0。
     加权值只是参考，官方怎么算以学校为准。

  ⑤ IB 1-7 → 4.0：IBO 官方**没有**换算公式，各机构表不同。
     这里取几张常见表（ Empowerly / ISHCMC / doris.school ）
     的公共交集：7=4.0  6=3.7  5=3.3  4=2.7  3=2.0  2=1.0  1=0.0
     （旧版 3=2.7、2=2.3、1=1.0 偏宽松：IB 1-2 是明显不及格，
     绝大多数表都落在 D/F 区间。）

  ⑥ 等比参考（gpa_linear）：每门 分数÷100×4，再按学分加权平均。
     ★ 这**不是**任何学校的通行算法 —— 它在中低分段系统性偏高
     （60 分不及格也能折出 2.4）。只作为平滑对照值展示，
     估算主值永远用 ②③ 的分段表。
────────────────────────────────────────────────
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# ---------- 换算表 ----------

# IB 1-7 等级 -> 4.0 制 GPA（依据见文件头 ⑤）
IB_TO_GPA: dict[int, float] = {7: 4.0, 6: 3.7, 5: 3.3, 4: 2.7, 3: 2.0, 2: 1.0, 1: 0.0}

# 字母等级 -> 4.0 制 GPA（标准表，A+ 与 A 同为 4.0 —— 多数学校不给 A+ 加分）
LETTER_TO_GPA: dict[str, float] = {
    "A+": 4.0, "A": 4.0, "A-": 3.7,
    "B+": 3.3, "B": 3.0, "B-": 2.7,
    "C+": 2.3, "C": 2.0, "C-": 1.7,
    "D+": 1.3, "D": 1.0, "D-": 0.7,
    "F": 0.0,
}

# 百分制分段 -> 4.0 制 GPA（从高到低匹配）
# 标准未加权 4.0 表：D 档 65-66 → 1.0，65 以下一律 F(0.0)
PERCENT_BANDS: list[tuple[float, float]] = [
    (93, 4.0), (90, 3.7), (87, 3.3), (83, 3.0), (80, 2.7),
    (77, 2.3), (73, 2.0), (70, 1.7), (67, 1.3), (65, 1.0),
    (0, 0.0),
]

# 加权加分：课程名里出现这些关键词时，加权 GPA 加分
#   AP / IB(HL) → +1.0；Honors / 荣誉 / SL → +0.5
#  只影响「加权 GPA」这一个参考字段，不影响未加权值。
BONUS_HIGH = 1.0
BONUS_MID = 0.5
_RE_BONUS_HIGH = re.compile(
    r"\b(AP|A\.?P\.?|HL|IB)\b|高等|大学先修", re.I)
_RE_BONUS_MID = re.compile(
    r"\b(SL|Honou?rs?)\b|荣誉|提高班", re.I)


def percent_to_letter(pct: float) -> str:
    """百分制 → 字母等级（与 PERCENT_BANDS 同一把尺子）。"""
    if pct >= 93:
        return "A"
    if pct >= 90:
        return "A-"
    if pct >= 87:
        return "B+"
    if pct >= 83:
        return "B"
    if pct >= 80:
        return "B-"
    if pct >= 77:
        return "C+"
    if pct >= 73:
        return "C"
    if pct >= 70:
        return "C-"
    if pct >= 67:
        return "D+"
    if pct >= 65:
        return "D"
    return "F"


_NUM = re.compile(r"-?\d+(?:\.\d+)?")
# 字母等级：前后不能紧邻字母数字，正确处理 +/- 后缀
_LETTER = re.compile(r"(?:^|[^A-Za-z0-9])([A-F])([+-])?(?![A-Za-z0-9])")
_IB_HINT = re.compile(r"\b(?:ib|level|lvl|grade|achievement|attain(?:ment)?)\b", re.I)

# 裸数字（无分母、无满分）落在 1-7 时，是否按 IB 等级解释
BARE_NUMBER_IS_IB_LEVEL = True


@dataclass
class ParsedScore:
    """一次成绩的解析结果。

    scale: 原始量纲 —— 'ib'（IB 1-7 等级）/ 'percent'（百分制）/ 'letter' / ''
    """
    raw: str = ""
    percent: float | None = None      # 归一化到 0-100
    gpa: float | None = None          # 归一化到 4.0
    level: int | None = None          # IB 等级（若适用）
    scale: str = ""
    display: str = ""                 # 展示用


def parse_score(raw: str, max_score: str = "") -> ParsedScore:
    """把成绩原文解析成归一化的百分制与 4.0 GPA。

    支持："6"（IB 等级）、"6/7"（IB 等级）、"45/50"（得分率）、"89%"、"A-"、"Level 6"。
    关键：分母为 7 且分子在 1-7 时按 **IB 等级**换算 GPA，而不是按百分比。
    """
    out = ParsedScore(raw=(raw or "").strip(), display=(raw or "").strip())
    s = out.raw
    if not s:
        return out

    max_v = _to_float(max_score)
    ib_hint = bool(_IB_HINT.search(s))

    # ---------- 形如 a/b ----------
    if "/" in s:
        left, _, right = s.partition("/")
        num, den = _to_float(left), _to_float(right)
        if num is None:
            return out
        if den and den > 0:
            out.percent = round(num / den * 100, 2)
            if _is_ib_level(num, den):
                _apply_ib(out, int(round(num)))
            else:
                out.gpa = percent_to_gpa(out.percent)
                out.scale = "percent"
            return out
        num_only = num
    else:
        num_only = None

    # ---------- 字母等级 ----------
    m = _LETTER.search(s.upper())
    if m and not _NUM.search(s[: m.start()]):
        letter = m.group(1) + (m.group(2) or "")
        if letter in LETTER_TO_GPA:
            out.gpa = LETTER_TO_GPA[letter]
            out.percent = gpa_to_percent(out.gpa)
            out.scale = "letter"
            return out

    # ---------- 数值 ----------
    v = num_only if num_only is not None else _to_float(s)
    if v is None:
        matches = _NUM.findall(s)
        if not matches:
            return out
        v = float(matches[-1])

    # 有满分数值 → 按得分率
    if max_v and max_v > 0 and v <= max_v:
        out.percent = round(v / max_v * 100, 2)
        if _is_ib_level(v, max_v):
            _apply_ib(out, int(round(v)))
        else:
            out.gpa = percent_to_gpa(out.percent)
            out.scale = "percent"
        return out

    # 1-7 且带 IB 提示词，或裸数字默认按 IB 等级
    if 1 <= v <= 7 and (ib_hint or num_only is not None or BARE_NUMBER_IS_IB_LEVEL):
        _apply_ib(out, int(round(v)))
        return out

    if 7 < v <= 100:
        out.percent = round(v, 2)
        out.gpa = percent_to_gpa(out.percent)
        out.scale = "percent"
        return out

    return out


def _is_ib_level(num: float, den: float) -> bool:
    """分母为 7、分子在 1-7 → 判定为 IB 等级。"""
    return abs(den - 7) < 1e-6 and 1 <= num <= 7


def _apply_ib(out: ParsedScore, level: int) -> None:
    out.level = level
    out.scale = "ib"
    out.gpa = IB_TO_GPA.get(level)
    # 百分比按 GPA 等价折算，保证同一行里「百分比」与「GPA」自洽
    out.percent = gpa_to_percent(out.gpa) if out.gpa is not None else None


def percent_to_gpa(pct: float) -> float:
    """百分制 → 4.0 制 grade points（逐门转换用，不是总平均）。"""
    for threshold, gpa in PERCENT_BANDS:
        if pct >= threshold:
            return gpa
    return 0.0


def gpa_to_percent(gpa: float) -> float:
    """4.0 制 GPA 反推一个代表性百分数。

    ★ 必须是**插值**，不能查表命中不到就兜底 ★

      原来写的是 ``table.get(round(gpa, 1), 75.0)``：
      表里只有 4.0 / 3.7 / 3.3 / 3.0 … 这些精确档位，
      而聚合出来的 GPA 几乎都是 3.56、3.42 这种中间值 ——
      一查就落空，于是**全部返回 75.0**。

      后果：任何「gpa → 百分数」的地方都拿到 75，
      「线性折算」的曲线被压成一条 3.000 的水平线，
      GPA 页与晚报里的百分数也可能被同一处污染。

      现在改成在档位之间做分段线性插值，两端做钳制。
    """
    table = [
        (0.0, 55.0), (0.7, 62.0), (1.0, 65.0), (1.3, 69.0),
        (1.7, 72.0), (2.0, 75.0), (2.3, 79.0), (2.7, 82.0),
        (3.0, 85.0), (3.3, 88.0), (3.7, 91.0), (4.0, 95.0),
    ]
    try:
        v = float(gpa)
    except (TypeError, ValueError):
        return table[0][1]
    if v <= table[0][0]:
        return table[0][1]
    if v >= table[-1][0]:
        return table[-1][1]
    for (x0, y0), (x1, y1) in zip(table, table[1:]):
        if x0 <= v <= x1:
            if x1 == x0:
                return y1
            return round(y0 + (y1 - y0) * (v - x0) / (x1 - x0), 2)
    return table[-1][1]

def _to_float(text: str) -> float | None:
    if not text:
        return None
    m = _NUM.search(text.replace(",", ""))
    if not m:
        return None
    try:
        return float(m.group())
    except ValueError:
        return None


# ---------- 单门课的 grade points ----------

def level_of(text: str) -> int | None:
    """从 '6' / 'Level 6' / '6/7' 这类文本里认出 IB 等级；认不出返回 None。"""
    p = parse_score(text)
    return p.level if p.scale == "ib" else None


def course_points(percent: float | None = None,
                  level: int | str | None = None,
                  letter: str = "") -> float | None:
    """一门课的 grade points（未加权，4.0 制）。

    取值顺序：百分制 > IB 等级 > 字母。
    ★ 0.5.1 修复：以前只有 percent 的课才计入 GPA，
      只有 IB 等级的课被整门漏掉 —— 这里补上兜底。
    """
    if percent is not None:
        try:
            return percent_to_gpa(float(percent))
        except (TypeError, ValueError):
            pass
    if level is not None and str(level).strip() != "":
        lv = level if isinstance(level, int) else level_of(str(level))
        if lv in IB_TO_GPA:
            return IB_TO_GPA[lv]
    if letter:
        return LETTER_TO_GPA.get(str(letter).strip().upper())
    return None


def course_bonus(course_name: str) -> float:
    """课程难度加分（用于加权 GPA）：AP/IB/HL +1.0，Honors/SL +0.5。"""
    name = course_name or ""
    if _RE_BONUS_HIGH.search(name):
        return BONUS_HIGH
    if _RE_BONUS_MID.search(name):
        return BONUS_MID
    return 0.0


def gpa_letter(points: float | None) -> str:
    """grade points → 字母（展示总 GPA 时用）。"""
    if points is None:
        return "—"
    best, best_d = "F", 99.0
    for letter, pts in LETTER_TO_GPA.items():
        d = abs(pts - points)
        if d < best_d:
            best, best_d = letter, d
    return best


# ---------- 汇总 ----------

def mean(values: list[float]) -> float | None:
    clean = [v for v in values if v is not None]
    if not clean:
        return None
    return round(sum(clean) / len(clean), 2)


def weighted_mean(items: list[tuple[float, float]]) -> float | None:
    """学分加权平均：items = [(points, credits), ...]。

    Σ(points × credits) / Σ(credits) —— 绝大多数学校的标准公式。
    credits ≤ 0 的项跳过；全部无效时返回 None。
    """
    total = 0.0
    wsum = 0.0
    for points, credits in items:
        if points is None:
            continue
        w = credits if credits and credits > 0 else 1.0
        total += points * w
        wsum += w
    if wsum <= 0:
        return None
    return round(total / wsum, 2)


def aggregate(courses: list[dict]) -> dict:
    """一组课程 → 总 GPA（未加权 + 加权 + 等比参考）。

    courses 的元素至少提供其一：
      percent (0-100) / level (IB 1-7 或文本) / letter ('A-')
      可选：credits（缺省 1.0 = 等权）、name（识别难度加分）

    返回 {gpa, gpa_weighted, gpa_linear, count, credits, mean_percent}
    —— count 是实际计入门数（三种量纲都认不出来的课不计入）。
      gpa_linear 是等比参考（分数÷100×4），非通行算法，仅供对照。
    """
    pts_w: list[tuple[float, float]] = []        # 未加权
    pts_wd: list[tuple[float, float]] = []       # 加权
    lin: list[tuple[float, float]] = []          # 等比参考
    pcts: list[float] = []
    credits_sum = 0.0

    for c in courses:
        if not isinstance(c, dict):
            continue
        p = course_points(c.get("percent"), c.get("level"), c.get("letter") or "")
        if p is None:
            continue
        cr = c.get("credits")
        try:
            cr = float(cr) if cr is not None and float(cr) > 0 else 1.0
        except (TypeError, ValueError):
            cr = 1.0
        pts_w.append((p, cr))
        pts_wd.append((min(p + course_bonus(str(c.get("name") or "")), 5.0), cr))
        credits_sum += cr
        pc = c.get("percent")
        if pc is None and c.get("level") is not None:
            # 只有等级的课：用 GPA 等价百分比参与「平均分」展示
            lv = c.get("level")
            lv = lv if isinstance(lv, int) else level_of(str(lv))
            g = IB_TO_GPA.get(lv)
            pc = gpa_to_percent(g) if g is not None else None
        if pc is not None:
            try:
                fv = float(pc)
                pcts.append(fv)
                #  等比参考：÷100×4（夹到 0-4），用同一份学分权重
                lin.append((max(0.0, min(fv / 25.0, 4.0)), cr))
            except (TypeError, ValueError):
                pass

    return {
        "gpa": weighted_mean(pts_w),
        "gpa_weighted": weighted_mean(pts_wd),
        "gpa_linear": weighted_mean(lin),
        "count": len(pts_w),
        "credits": round(credits_sum, 2),
        "mean_percent": round(sum(pcts) / len(pcts), 2) if pcts else None,
    }
