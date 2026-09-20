"""数据模型 —— 字段严格对应 ManageBac 网页上**实际显示**的内容。

设计原则：网页给什么就存什么，不臆造、不重算。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Task:
    """一次作业/任务（来自课程页的 core_tasks 卡片）。"""

    title: str = ""
    url: str = ""
    course: str = ""
    course_url: str = ""
    class_id: str = ""

    # ---- 日期 ----
    due_date: str = ""        # ISO 日期 YYYY-MM-DD（尽量推断）
    due_text: str = ""        # 网页原文，如 "Tuesday at 3:40 PM"
    # 日期可信度：badge=页面上有明确日历徽章（权威）
    #             text=仅凭星期文字推断（不可靠，可能被覆盖）
    date_source: str = ""
    publish_date: str = ""    # ISO（网页若给出）
    publish_text: str = ""
    days_left: int | None = None

    # ---- 分类 ----
    category: str = ""        # Summative / Formative
    kind: str = ""            # Quiz / Homework / Essay ...

    # ---- 成绩（网页显示值）----
    score: str = ""           # 原文，如 "A 100 / 100 pts"
    level: str = ""           # 等级 B / A ...
    percent: float | None = None
    score_num: float | None = None    # 得分，如 100
    score_max: float | None = None    # 满分，如 100
    graded: bool = False

    # ---- 内容 ----
    #  是否有附件（供界面决定「只给有附件的显示下载键」）
    has_attachment: bool = False
    # 老师要求（任务详情页的说明文本，抓一次存下来）
    description: str = ""

    # ---- 状态 ----
    status: str = ""          # 已提交 / 待提交 / 迟交
    submitted: bool = False
    late: bool = False
    pending: bool = False
    #  是否算「已完成」—— 用户定义的规则：已提交文件或老师已给分
    completed: bool = False
    #  用户手动标记为「我已完成，不再提示」
    #   （用于线下交纸质版、老师未给分、线上也无提交徽章的情况）
    manual: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CategoryAverage:
    """Task Category Averages 里的一行：分类 + 权重 + 等级 + 得分。"""

    name: str = ""            # Quiz / Homework ...
    weight: float | None = None   # 20 -> 20%
    level: str = ""           # B / C / A ...
    percent: float | None = None
    graded: bool = False      # 是否已出分（未出分显示 "-"）

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Course:
    """一门课程。"""

    name: str = ""
    url: str = ""
    class_id: str = ""

    # 总评（网站算好的）
    overall_level: str = ""       # B
    overall_percent: float | None = None   # 85.46

    categories: list[CategoryAverage] = field(default_factory=list)
    tasks: list[Task] = field(default_factory=list)

    # 任务完成统计（来自 Overall Task Completion）
    submitted_count: int = 0
    late_count: int = 0
    pending_count: int = 0

    teacher: str = ""

    def to_dict(self) -> dict:
        d = {k: v for k, v in asdict(self).items() if k != "tasks"}
        d["tasks"] = [t.to_dict() for t in self.tasks]
        return d


# ==================================================================
# 课表（来自第二个网站「教学管理系统」）
# ==================================================================

@dataclass
class Lesson:
    """一节课。"""

    subject: str = ""         # 课程名
    teacher: str = ""         # 教师
    room: str = ""            # 教室
    start: str = ""           # ISO datetime，如 2026-09-18T08:00
    end: str = ""             # ISO datetime
    day: str = ""             # YYYY-MM-DD
    period: str = ""          # 第几节
    kind: str = ""            # 正课 / 晚自习
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Schedule:
    """课表快照。

     periods：节次时间表（P1..P9 的起止时间）。
      用于课表页「空档填充」—— 没课的节次显示为「自习」占位，
      而不是直接跳过（跳过就看不出中间有空）。
      抓不到时是空列表，前端会退回「不填充」的行为。
    """

    source: str = ""              # 数据来源标识
    updated: str = ""
    ready: bool = False           # 是否已成功接入
    error: str = ""
    lessons: list[Lesson] = field(default_factory=list)
    periods: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "updated": self.updated,
            "ready": self.ready,
            "error": self.error,
            "lessons": [l.to_dict() for l in self.lessons],
            "periods": list(self.periods),
        }
