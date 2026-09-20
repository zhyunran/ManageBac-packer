"""解析 ManageBac 页面的真实结构。

【依据】以下选择器全部来自真实页面快照（data/recon/）实测，不是猜测：

  任务卡片（课程主页）
    <div class="fusion-card-item short-assignment section ...">
      <div class="date-badge"><div class="month">Sep</div><div class="day">23</div></div>
      <div class="h4 title"><a href="/student/classes/{cid}/core_tasks/{tid}">标题</a></div>
      <div class="labels-set"><div class="label">Summative</div><div class="label">Quiz</div></div>
      <div class="due-date"><div class="due regular">Tuesday at 3:40 PM</div></div>
    </div>
    <div class="assessment task-score ... assessment-cell">
      <div class="label label-not-applicable">N/A</div>   ← 或 label > B (85.46%)
    </div>

  分类成绩（/units 页）
    <section>
      <h6>Task Category Averages</h6>
      <div class="sidebar-items-list">
        <div class="list-item list-item-head">
          <div class="cell">Category (Weight)</div><div class="cell">Mark (Score)</div>
        </div>
        <div class="list-item">
          <div class="cell">Quiz (20%)</div>
          <div class="cell"><strong>C</strong> (76.67%)</div>
        </div>
      </div>
    </section>

  任务完成统计（/units 页）
    "Overall Task Completion ... 3 Submitted - 2 Late - 0 Pending"
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta

from bs4 import BeautifulSoup

from . import config
from .models import CategoryAverage, Course, Task

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- 正则

MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
          "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12}

WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
            "friday": 4, "saturday": 5, "sunday": 6}

# "8:50 AM " / "12:05 PM " 之类的前缀（周历表格里会混进标题）
TIME_PREFIX_RE = re.compile(r"^\s*\d{1,2}:\d{2}\s*(?:AM|PM)\s*", re.I)
# "B (85.46%)" / "C (76.67%)"
GRADE_PCT_RE = re.compile(r"^([A-F][+-]?)\s*\(\s*([\d.]+)\s*%\s*\)$")
# "Quiz (20%)"
CAT_WEIGHT_RE = re.compile(r"^(.*?)\s*\(\s*([\d.]+)\s*%\s*\)$")
# "Tuesday at 3:40 PM"
DUE_TEXT_RE = re.compile(r"(\w+day)\s+at\s+(\d{1,2}):(\d{2})\s*(AM|PM)", re.I)
# "September 15, 2026"
LONG_DATE_RE = re.compile(r"([A-Z][a-z]+)\s+(\d{1,2}),\s*(\d{4})")


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()


def clean_title(s: str) -> str:
    """去掉标题里混入的时间前缀（周历表格导致）。"""
    t = clean(s)
    prev = None
    while prev != t:                 # 可能叠加多层
        prev = t
        t = TIME_PREFIX_RE.sub("", t).strip()
    return t


# ---------------------------------------------------------------- 附件

# 卡片里表示「有附件」的线索。
#
# 【实测结构】附件出现在任务卡片的**描述块**里：
#     <div class='h4'>Description</div>
#     <div class="show-more" ...>
#       <div class="redactor-styles fr-view fr-element">
#         <p>请周一早自习将纸质版交给科代表…</p>
#         <a href="…/attachments/…" class="fr-file"
#            data-name="Pre-Physics_Homework2.pdf" target="blank">
#           <span><span class="fr-inner">Pre-Physics_Homework2.pdf</span>
#                 <span class="fr-file-size">137.2 KB</span></span>
#         </a>
#       </div>
#     </div>
#
#  关键标志：`class="fr-file"` + `data-name` 属性
#   注意不要用 "clip" 之类做判据 —— 页面的 SVG 里到处是 `clip-path`，
#   会造成大量误判（早期版本就踩过这个坑）。
_ATTACH_CLASSES = ("fr-file", "fr-attachment")


def _card_has_attachment(card) -> bool:
    """判断任务卡片里是否带附件。

     用户要求「只给有附件的任务显示下载键」，
      所以宁可漏判也不能误判 —— 只认明确的 `fr-file` 标志。
    """
    if card is None:
        return False

    # 1. 直接的附件链接（最可靠）
    for a in card.find_all("a", href=True):
        cls = " ".join(a.get("class") or [])
        if any(c in cls for c in _ATTACH_CLASSES):
            return True
        if "/attachments/" in (a.get("href") or ""):
            return True
        # data-name 形如 xxx.pdf
        nm = a.get("data-name") or ""
        if nm and re.search(r"\.\w{2,5}$", nm.strip()):
            return True

    return False


# ---------------------------------------------------------------- 提交状态

# 徽章原文 → 判定（注意 "Not Submitted" 含 "Submitted"，必须先判否定）
_PENDING_WORDS = ("not submitted", "pending", "waiting", "未提交", "待提交", "未完成")
_LATE_WORDS = ("late", "overdue", "迟交", "逾期")


def parse_status(card) -> tuple[str, bool, bool, bool]:
    """解析任务的提交状态。

    依据（真实结构，见解析规则.md）：
        <span class="badge color-box-green" data-bs-title="3 days early">
          <span class="badge-label">Submitted</span></span>
        <span class="badge color-box-gray" data-bs-title="Waiting">
          <span class="badge-label">Pending</span></span>
        <span class="cell not-submitted">Not Submitted</span>

    返回 (状态文本, 已提交, 待提交, 迟交)
    """
    if card is None:
        return "", False, False, False

    parts: list[str] = []
    for el in card.select(".labels-set .badge .badge-label"):
        parts.append(clean(el.get_text(" ")))
    for el in card.select(".badge[data-bs-title]"):
        parts.append(clean(el.get("data-bs-title") or ""))
    for el in card.select(".assessment-cell .cell"):
        t = clean(el.get_text(" "))
        if t:
            parts.append(t)

    blob = " | ".join(p for p in parts if p).lower()
    if not blob:
        return "", False, False, False

    if any(w in blob for w in _LATE_WORDS):
        return "迟交", False, False, True
    if any(w in blob for w in _PENDING_WORDS):
        return "待提交", False, True, False
    if "submitted" in blob or "已提交" in blob:
        return "已提交", True, False, False
    return "", False, False, False


# ---------------------------------------------------------------- 日期

def resolve_year(month: int, day: int, today: date | None = None) -> int:
    """给「月/日」补上年份（页面不含年份）。

    策略：**取离今天最近的那个**。这一个规则同时满足两类情况：

        今天 9/17，徽章 "Sep 11"
            2026-09-11 (-6 天)  ← 最近，选它（刚过去的任务）
            2027-09-11 (+359)
        今天 9/17，徽章 "Jan 17"
            2026-01-17 (-243)
            2027-01-17 (+122)   ← 最近，选它（跨年的未来任务）

    注意：不能简单地「优先未来」——那会把 Sep 11 错判成明年；
    也不能用固定窗口（如 ±180 天）——那会让 Jan 17 退化成已过去。
    """
    today = today or date.today()
    candidates: list[date] = []
    for year in (today.year - 1, today.year, today.year + 1, today.year + 2):
        try:
            candidates.append(date(year, month, day))
        except ValueError:
            continue
    if not candidates:
        return today.year
    return min(candidates, key=lambda d: abs((d - today).days)).year


def to_iso(year: int, month: int, day: int) -> str:
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return ""


def parse_due_text(text: str, ref: date | None = None) -> str:
    """把 "Tuesday at 3:40 PM" 解析成 ISO 日期。

    依据参考日（通常来自 date-badge）找最近的那个该星期几。
    """
    m = DUE_TEXT_RE.search(text or "")
    if not m:
        return ""
    wd = WEEKDAYS.get(m.group(1).lower())
    if wd is None:
        return ""
    ref = ref or date.today()
    delta = (wd - ref.weekday()) % 7
    if delta == 0:
        delta = 0            # 就是今天
    # 若推算出的日期比参考日早很多（跨周），取下一个
    return (ref + timedelta(days=delta)).isoformat()


def days_until(iso: str) -> int | None:
    if not iso:
        return None
    try:
        return (date.fromisoformat(iso) - date.today()).days
    except ValueError:
        return None


# ---------------------------------------------------------------- 抓取器

class Scraper:
    """页面抓取器。

    只要传入的对象满足：
        session.page() -> page，且 page 有
        goto(url) / wait_for_timeout(ms) / content()
    即可工作。

    因此 `cdp.Session`（浏览器）与 `httpclient.HttpSession`（纯 HTTP）
    **都能直接用**。
    """

    def __init__(self, session) -> None:
        self.session = session
        self.page = session.page()
        self.report: dict = {"courses": [], "notes": []}
        #  页面若明确给出官方 GPA 就存在这里（找不到就是 None，绝不臆造）
        self.official_gpa: dict | None = None

    def goto(self, url: str, wait_ms: int = 2200) -> str:
        self.page.goto(url)
        self.page.wait_for_timeout(wait_ms)
        return self.page.content()

    # ============================ 课程列表 ============================

    def fetch_courses(self) -> list[Course]:
        """从 /student/classes 读取课程；名称取导航菜单（最完整）。"""
        html = self.goto(f"{config.BASE_URL}/student/classes")
        soup = BeautifulSoup(html, "html.parser")

        #  顺手找一下官方 GPA（这个页面最可能出现）
        #   找不到就保持 None —— 绝不臆造（借鉴 CampusDesk 的原则）
        if self.official_gpa is None:
            g = self.parse_official_gpa(soup)
            if g:
                self.official_gpa = g
                log.info("发现页面公布的官方 GPA：%s / %s",
                         g["value"], g["scale"])

        courses: list[Course] = []
        seen: set[str] = set()

        # 优先用侧边栏课程菜单 —— 名称带完整信息（含班级、教室）
        for a in soup.select(".f-menu__submenu-link, .f-menu__nav-link"):
            href = a.get("href") or ""
            m = re.search(r"/student/classes/(\d+)", href)
            if not m:
                continue
            cid = m.group(1)
            if cid in seen:
                continue
            name = clean(a.get_text(" "))
            # 去掉菜单里的额外标记
            name = re.sub(r"^[\s\d+]+", "", name)
            if not name:
                continue
            seen.add(cid)
            courses.append(Course(name=name,
                url=f"{config.BASE_URL}/student/classes/{cid}",
                class_id=cid,
            ))

        if not courses:
            # 退路：通用链接扫描
            for a in soup.find_all("a", href=True):
                m = re.search(r"/student/classes/(\d+)", a["href"])
                if not m or m.group(1) in seen:
                    continue
                seen.add(m.group(1))
                courses.append(Course(name=clean(a.get_text(" ")) or m.group(1),
                    url=f"{config.BASE_URL}/student/classes/{m.group(1)}",
                    class_id=m.group(1),
                ))

        self.report["notes"].append(f"发现 {len(courses)} 门课程")
        return courses

    # ============================ 单门课程 ============================

    def fetch_course_detail(self, course: Course) -> Course:
        """抓一门课：任务明细（两处来源合并）+ /units 分类成绩。

        任务来源有两个，各自覆盖不同场景：
          1. 课程主页     —— 含近期任务，但混有周历表格
          2. /core_tasks  —— 完整历史任务列表，标题干净
        两者按任务 ID 合并，优先保留有日期的一方。
        """
        merged: dict[str, Task] = {}

        # ---- 1. 课程主页 ----
        try:
            html = self.goto(course.url)
            soup = BeautifulSoup(html, "html.parser")
            self._merge_tasks(merged, self._parse_tasks(soup, course), course)
            course.teacher = self._parse_teacher(soup)
        except Exception as e:
            self.report["notes"].append(f"{course.name} 主页失败：{e}")

        # ---- 2. /core_tasks 完整列表 ----
        try:
            html = self.goto(course.url + "/core_tasks")
            soup = BeautifulSoup(html, "html.parser")
            self._merge_tasks(merged, self._parse_tasks(soup, course), course)
        except Exception as e:
            self.report["notes"].append(f"{course.name} /core_tasks 失败：{e}")

        course.tasks = list(merged.values())

        # ---- 3. /units：分类成绩 + 完成统计 ----
        try:
            html = self.goto(course.url + "/units")
            soup = BeautifulSoup(html, "html.parser")
            course.categories = self._parse_categories(soup)
            self._parse_completion(soup, course)
            self._merge_overall(course)
        except Exception as e:
            self.report["notes"].append(f"{course.name} /units 失败：{e}")

        self.report["courses"].append({
            "name": course.name, "id": course.class_id,
            "tasks": len(course.tasks), "categories": len(course.categories),
        })
        return course

    @staticmethod
    def _merge_tasks(merged: dict[str, Task], new_tasks: list[Task],
                     course: Course) -> None:
        """按任务 ID 合并；新数据在「日期/成绩/状态」上更完整时补全旧数据。"""
        for t in new_tasks:
            m = re.search(r"/core_tasks/(\d+)", t.url)
            key = m.group(1) if m else t.url
            old = merged.get(key)
            if old is None:
                merged[key] = t
                continue
            # 补全缺失字段（不覆盖已有的非空值）
            #  日期：badge（明确日历日期）比 text（只有星期）权威，可覆盖
            rank = {"": 0, "text": 1, "badge": 2}
            if t.due_date and rank.get(t.date_source, 0) > rank.get(old.date_source, 0):
                old.due_date = t.due_date
                old.date_source = t.date_source
                if t.due_text:
                    old.due_text = t.due_text
            elif not old.due_date and t.due_date:
                old.due_date = t.due_date
                old.date_source = t.date_source
                if not old.due_text and t.due_text:
                    old.due_text = t.due_text
            elif not old.due_text and t.due_text:
                old.due_text = t.due_text
            if not old.graded and t.graded:
                old.score = t.score
                old.level = t.level
                old.percent = t.percent
                old.graded = t.graded
            if not old.category and t.category:
                old.category = t.category
            if not old.kind and t.kind:
                old.kind = t.kind
            if not old.course and t.course:
                old.course = t.course
            #  状态：只要任一处显示已提交/待提交，就采纳
            if not old.status and t.status:
                old.status = t.status
            if t.submitted:
                old.submitted = True
                old.status = old.status or "已提交"
            if t.late:
                old.late = True
            if t.pending and not old.submitted:
                old.pending = True
            # 标题取更干净的
            if len(clean_title(t.title)) > len(clean_title(old.title)):
                old.title = t.title
            old.days_left = days_until(old.due_date)
            old.completed = bool(old.submitted or old.graded)

    # ---------------------------- 任务 ----------------------------

    def _parse_tasks(self, soup: BeautifulSoup, course: Course) -> list[Task]:
        """解析页面里的 core_tasks 链接为任务。

        注意：周历表格（table.f-tw-calendar）里的链接标题会混入时间前缀，
        且其日期在表头列而非任务本身 —— 因此这类链接只取标题，
        日期交由 /core_tasks 页面提供。
        """
        tasks: list[Task] = []
        seen: set[str] = set()

        for a in soup.find_all("a", href=True):
            m = re.search(r"/core_tasks/(\d+)", a["href"])
            if not m:
                continue
            tid = m.group(1)
            title = clean_title(a.get_text(" "))
            if not title or tid in seen:
                continue
            seen.add(tid)

            # 是否位于周历表格内
            in_calendar = bool(a.find_parent("table", class_=lambda c: c and "calendar" in " ".join(c)))

            # ---- 精确定位「卡片」与「成绩单元格」----
            # 实测结构：
            #   <div class="fusion-card-item short-assignment ...">  ← 任务卡
            #       ... 标题 / 日期徽章 / 分类标签 / 提交徽章 ...
            #   </div>
            #   <div class="assessment task-score ... assessment-cell"> ← 成绩（兄弟节点！）
            #   </div>
            # 关键：成绩单元格是【卡片的兄弟】，不是子节点 —— 早期版本
            # 只向上找父级，因此永远读不到成绩，导致已完成任务被当成待办。
            card = None
            if not in_calendar:
                node = a
                for _ in range(8):
                    p = node.parent
                    if p is None or p.name in ("body", "html"):
                        break
                    cls = " ".join(p.get("class") or [])
                    if "fusion-card-item" in cls or "short-assignment" in cls:
                        card = p
                        break
                    node = p
                if card is None:
                    # 退化：用旧策略（向上找带 date-badge 的容器）
                    node = a
                    for _ in range(7):
                        p = node.parent
                        if p is None or p.name in ("body", "html"):
                            break
                        node = p
                        if (node.select_one(".assessment-cell")
                                or node.select_one(".date-badge")):
                            break
                    card = node

            task = Task(title=title,
                url=f"{config.BASE_URL}/student/classes/{course.class_id}/core_tasks/{tid}",
                course=course.name,
                course_url=course.url,
                class_id=course.class_id,
            )

            if not in_calendar and card is not None:
                #  是否有附件（卡片里出现 paperclip / 附件图标）
                task.has_attachment = _card_has_attachment(card)

                # 日期徽章（权威来源：明确的日历日期）
                badge = card.select_one(".date-badge")
                ref: date | None = None
                if badge:
                    mon = clean((badge.select_one(".month") or badge).get_text(" "))
                    day_el = badge.select_one(".day")
                    day_txt = clean(day_el.get_text(" ")) if day_el else ""
                    mo = MONTHS.get(mon[:4].lower()) or MONTHS.get(mon[:3].lower())
                    dm = re.search(r"\d{1,2}", day_txt)
                    if mo and dm:
                        ref = date(resolve_year(mo, int(dm.group())), mo, int(dm.group()))
                        task.due_date = ref.isoformat()
                        task.date_source = "badge"

                # 截止时间文本（仅作兜底：只有星期，没有日期）
                due_el = card.select_one(".due-date .due, .due")
                if due_el:
                    task.due_text = clean(due_el.get_text(" "))
                    if not task.due_date and ref is not None:
                        task.due_date = parse_due_text(task.due_text, ref)
                        task.date_source = "text"
                    elif not task.due_date:
                        task.due_date = parse_due_text(task.due_text)
                        task.date_source = "text"

                # 分类标签（labels-set 里也可能有提交徽章，故只取 div.label）
                labels = [clean(l.get_text(" ")) for l in card.select(".labels-set .label")]
                labels = [l for l in labels if l]
                if labels:
                    task.category = labels[0]
                    if len(labels) > 1:
                        task.kind = labels[1]

                #  提交状态（来自卡片内的 badge / not-submitted 单元格）
                status, submitted, pending, late = parse_status(card)
                task.status, task.submitted = status, submitted
                task.pending, task.late = pending, late

                #  成绩：优先在卡片内找；找不到再找【兄弟节点】
                cell = card.select_one(".assessment-cell")
                if cell is None:
                    cell = self._find_sibling_cell(card)
                if cell is not None:
                    self._apply_grade(task, self._cell_text(cell))

            task.days_left = days_until(task.due_date)
            #  完成判定：已提交或老师已给分（用户明确要求）
            task.completed = bool(task.submitted or task.graded)
            tasks.append(task)

        return tasks

    @staticmethod
    def _find_sibling_cell(card):
        """成绩单元格常与任务卡同级（兄弟节点）。向后找若干个兄弟。"""
        if card is None:
            return None
        nxt = card
        for _ in range(4):
            nxt = nxt.find_next_sibling()
            if nxt is None:
                break
            try:
                if "assessment-cell" in " ".join(nxt.get("class") or []):
                    return nxt
                found = nxt.select_one(".assessment-cell")
                if found is not None:
                    return found
            except Exception:
                continue
        return None

    @staticmethod
    def _cell_text(cell) -> str:
        """把成绩单元格的文本规范化。

        实测形态：
            "A 100 / 100 pts" / "D 65 / 100 pts" / "B 82 / 100 pts"
            "Not Assessed Yet" / "N/A"
        """
        if cell is None:
            return ""
        return clean(cell.get_text(" "))

    @staticmethod
    def _apply_grade(task: Task, text: str) -> None:
        """解析成绩单元格文本（网页显示什么就存什么）。

        实测到的真实形态：
            "A 100 / 100 pts"      → 等级 A，得分 100/100
            "D 65 / 100 pts"       → 等级 D，得分 65/100
            "B 82 / 100 pts"       → 等级 B
            "A (95.00%)"           → 等级 A，百分比 95
            "Not Assessed Yet"     → 未评分（不算完成）
            "N/A" / "-"            → 无成绩
        """
        if not text:
            return
        raw = clean(text)
        low = raw.lower()

        # ---- 未评分：明确不标记 graded ----
        if any(k in low for k in ("not assessed", "not graded", "ungraded",
                                  "not submitted", "n/a", "待评分")):
            task.score = raw
            task.graded = False
            return
        if raw in ("-", "—", "–", ""):
            return

        task.score = raw

        # ---- 形态 1：等级 + 得分/满分 + pts ----
        m = re.search(r"([A-F][+-]?)\s*(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)",
                      raw, re.I)
        if m:
            task.level = m.group(1).upper()
            task.score_num = float(m.group(2))
            task.score_max = float(m.group(3))
            if task.score_max:
                task.percent = round(task.score_num / task.score_max * 100, 2)
            task.graded = True
            return

        # ---- 形态 2：只要有 a/b ----
        m2 = re.search(r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)", raw)
        if m2:
            task.score_num = float(m2.group(1))
            task.score_max = float(m2.group(2))
            if task.score_max:
                task.percent = round(task.score_num / task.score_max * 100, 2)
            lv = re.match(r"^([A-F][+-]?)", raw, re.I)
            if lv:
                task.level = lv.group(1).upper()
            task.graded = True
            return

        # ---- 形态 3：等级 (百分比) ----
        m3 = GRADE_PCT_RE.match(raw)
        if m3:
            task.level = m3.group(1)
            try:
                task.percent = float(m3.group(2))
            except ValueError:
                pass
            task.graded = True
            return

        # ---- 形态 4：仅百分比 ----
        mp = re.search(r"([\d.]+)\s*%", raw)
        if mp:
            try:
                task.percent = float(mp.group(1))
            except ValueError:
                pass
            lv = re.match(r"^([A-F][+-]?)", raw, re.I)
            if lv:
                task.level = lv.group(1).upper()
            task.graded = True
            return

        # ---- 形态 5：仅等级 ----
        ml = re.match(r"^([A-F][+-]?)$", raw.strip(), re.I)
        if ml:
            task.level = ml.group(1).upper()
            task.graded = True

    @staticmethod
    def _parse_teacher(soup: BeautifulSoup) -> str:
        for el in soup.find_all(string=re.compile(r"^\s*Teachers?\s*$", re.I)):
            sec = el.find_parent(["section", "div"])
            if sec:
                text = clean(sec.get_text(" "))
                text = re.sub(r"^Teachers?\s*", "", text)
                if text:
                    return text[:80]
        return ""

    # ---------------------------- 分类成绩 ----------------------------

    def _parse_categories(self, soup: BeautifulSoup) -> list[CategoryAverage]:
        """解析 Task Category Averages 列表。"""
        out: list[CategoryAverage] = []

        anchor = None
        for el in soup.find_all(string=re.compile("Task Category Averages", re.I)):
            anchor = el.find_parent(["section", "div", "article"])
            break
        if anchor is None:
            return out

        # 在锚点所在的 section 里找 list-item
        section = anchor
        for _ in range(3):
            if section.select(".list-item"):
                break
            if section.parent is None:
                break
            section = section.parent

        for item in section.select(".list-item"):
            cells = item.select(".cell")
            if len(cells) < 2:
                continue
            name_txt = clean(cells[0].get_text(" "))
            val_txt = clean(cells[1].get_text(" "))
            if name_txt in ("Category (Weight)", "Mark (Score)"):
                continue

            cat = CategoryAverage()
            mw = CAT_WEIGHT_RE.match(name_txt)
            if mw:
                cat.name = mw.group(1).strip()
                try:
                    cat.weight = float(mw.group(2))
                except ValueError:
                    pass
            else:
                cat.name = name_txt

            mg = GRADE_PCT_RE.match(val_txt)
            if mg:
                cat.level = mg.group(1)
                try:
                    cat.percent = float(mg.group(2))
                except ValueError:
                    pass
                cat.graded = True
            elif val_txt in ("-", "—", "") or val_txt.startswith("-"):
                cat.graded = False
            else:
                mp = re.search(r"([\d.]+)\s*%", val_txt)
                if mp:
                    try:
                        cat.percent = float(mp.group(1))
                        cat.graded = True
                    except ValueError:
                        pass

            if cat.name:
                out.append(cat)

        return out

    @staticmethod
    def _parse_completion(soup: BeautifulSoup, course: Course) -> None:
        """解析 'Overall Task Completion' 的已交/迟交/待交数量。"""
        for el in soup.find_all(string=re.compile("Overall Task Completion", re.I)):
            sec = el.find_parent(["section", "div"])
            for _ in range(4):
                if sec is None:
                    break
                text = clean(sec.get_text(" "))
                m = re.search(r"(\d+)\s*Submitted\s*[-–]?\s*(\d+)\s*Late\s*[-–]?\s*(\d+)\s*Pending",
                    text, re.I)
                if m:
                    course.submitted_count = int(m.group(1))
                    course.late_count = int(m.group(2))
                    course.pending_count = int(m.group(3))
                    return
                sec = sec.parent
            break

    # ---- 总评：优先用网页显示的 Overall ----

    @staticmethod
    def _merge_overall(course: Course) -> None:
        overall = next((c for c in course.categories
                        if c.name.strip().lower() == "overall"), None)
        if overall:
            course.overall_level = overall.level
            course.overall_percent = overall.percent
        # 若网页没给 Overall，则不臆造 —— 保持空白

    # ---- 官方 GPA（页面若明确给了就用它）----
    #
    #  借鉴 CampusDesk 的做法：
    #     · 只接受**紧凑、明确标注、带 scale** 的行（如 "GPA: 3.8 / 4.0"）
    #     · 分子必须 ≤ 分母，分母必须 > 0
    #     · 找不到就是找不到，**绝不臆造**
    #
    #   这么严格是为了避免把「某次作业的 3.8/4」误当成「总 GPA」。
    #   （CampusDesk 的 README 里专门写了这条教训）
    _GPA_ROW_RE = re.compile(r"(?:Cumulative\s+GPA|Overall\s+GPA|累计\s*GPA|平均绩点|总\s*GPA)"
        r"\s*[:：]?\s*(\d{1,2}(?:\.\d+)?)\s*/\s*(\d{1,2}(?:\.\d+)?)",
        re.I,
    )

    def parse_official_gpa(self, soup) -> dict | None:
        """从页面里找「官方 GPA」。找不到返回 None。

        只认这种形状（一行之内、明确标注、带分母）：
            "Cumulative GPA: 3.85 / 4.0"
            "总 GPA 3.9/4"
        """
        if soup is None:
            return None
        try:
            for el in soup.select("tr, .list-item, li, div, p"):
                txt = clean(el.get_text(" "))
                #  长度限制：避免把一大段文字里的片段误当 GPA 行
                if not txt or len(txt) > 120:
                    continue
                m = self._GPA_ROW_RE.search(txt)
                if not m:
                    continue
                num = float(m.group(1))
                den = float(m.group(2))
                #  数值合理性：分子 ≤ 分母，分母 > 0 且在常见范围内
                if den <= 0 or num > den or den > 10 or num < 0:
                    continue
                label = clean(m.group(0).split(":")[0].split("：")[0]).strip()
                return {"value": num, "scale": den,
                        "label": label or "官方 GPA", "source": "site"}
        except Exception as e:
            log.warning("解析官方 GPA 失败：%s", e)
        return None

    # ============================ 汇总 ============================

    def save_report(self) -> None:
        config.ensure_dirs()
        import json
        (config.DATA_DIR / "scrape_report.json").write_text(json.dumps(self.report, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- 汇总统计

def summarize_courses(courses: list[Course],
                      official_gpa: dict | None = None) -> dict:
    """按网页给出的百分比计算跨课程平均（仅作参考，不改写单科数值）。

     借鉴 CampusDesk：**把「官网公布的 GPA」和「我们估算的」分开**。
        官方值来自页面明确标注的行（如 "Cumulative GPA: 3.85 / 4.0"），
        我们的估算值只作为补充，且界面上要标明「估算」。

    估算规则（简单等权，与 digest.py 的换算表一致）：
        93+ → 4.0, 90+ → 3.7, 87+ → 3.3, 83+ → 3.0,
        80+ → 2.7, 77+ → 2.3, 73+ → 2.0, 70+ → 1.7,
        67+ → 1.3, 63+ → 1.0, 60+ → 0.7, 其余 0
    """
    pcts = [c.overall_percent for c in courses if c.overall_percent is not None]
    levels = [c.overall_level for c in courses if c.overall_level]
    mean = round(sum(pcts) / len(pcts), 2) if pcts else None

    # —— 估算 GPA（简单等权）——
    def _est(p: float) -> float:
        for lo, g in ((93, 4.0), (90, 3.7), (87, 3.3), (83, 3.0), (80, 2.7),
                      (77, 2.3), (73, 2.0), (70, 1.7), (67, 1.3), (63, 1.0),
                      (60, 0.7)):
            if p >= lo:
                return g
        return 0.0

    est = None
    if pcts:
        est = round(sum(_est(p) for p in pcts) / len(pcts), 2)

    return {
        "course_count": len(courses),
        "graded_count": len(pcts),
        "mean_percent": mean,
        "levels": levels,
        #  两个字段分开，不混为一谈
        "gpa_official": official_gpa,            # 页面公布的（可能为 None）
        "gpa_estimated": est,                    # 我们估算的
        "gpa_estimated_count": len(pcts),
    }
