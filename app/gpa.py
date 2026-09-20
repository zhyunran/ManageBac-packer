"""成绩解析与 GPA 换算。

换算规则全部集中在下方常量里，按你学校的规定改这里即可。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# ---------- 换算表 ----------

# IB 1-7 等级 -> 4.0 制 GPA
IB_TO_GPA: dict[int, float] = {7: 4.0, 6: 3.7, 5: 3.3, 4: 3.0, 3: 2.7, 2: 2.3, 1: 1.0}

# 字母等级 -> 4.0 制 GPA
LETTER_TO_GPA: dict[str, float] = {
    "A+": 4.0, "A": 4.0, "A-": 3.7,
    "B+": 3.3, "B": 3.0, "B-": 2.7,
    "C+": 2.3, "C": 2.0, "C-": 1.7,
    "D+": 1.3, "D": 1.0, "D-": 0.7,
    "F": 0.0,
}

# 百分制分段 -> 4.0 制 GPA（从高到低匹配）
PERCENT_BANDS: list[tuple[float, float]] = [
    (93, 4.0), (90, 3.7), (87, 3.3), (83, 3.0), (80, 2.7),
    (77, 2.3), (73, 2.0), (70, 1.7), (67, 1.3), (63, 1.0),
    (60, 0.7), (0, 0.0),
]

# 百分制分段的字母等价（用于展示）
def percent_to_letter(pct: float) -> str:
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
    if pct >= 63:
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
    for threshold, gpa in PERCENT_BANDS:
        if pct >= threshold:
            return gpa
    return 0.0


def gpa_to_percent(gpa: float) -> float:
    """4.0 制 GPA 反推一个代表性百分数（仅用于图表对齐）。"""
    table = {4.0: 95.0, 3.7: 91.0, 3.3: 88.0, 3.0: 85.0, 2.7: 82.0,
             2.3: 79.0, 2.0: 75.0, 1.7: 72.0, 1.3: 69.0, 1.0: 65.0, 0.7: 62.0, 0.0: 55.0}
    return table.get(round(gpa, 1), 75.0)


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


# ---------- 汇总 ----------

def mean(values: list[float]) -> float | None:
    clean = [v for v in values if v is not None]
    if not clean:
        return None
    return round(sum(clean) / len(clean), 2)
