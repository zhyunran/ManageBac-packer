"""课表抓取 —— 直接从 Python 调用 API（带 Bearer Token，绕开 CORS）。

【原理】
    yly.seiue.com 是 React SPA，它请求 api.seiue.com 时带
    ``Authorization: Bearer <JWT>``。该 token 存放在浏览器 localStorage 的
    ``persist:root`` → ``session`` → ``oAuthToken.accessToken``。

    登录由**你自己**在真实 Edge 窗口里完成（见 schedule_login.py）；
    本模块只**读取** token 然后直接从 Python 发 HTTPS 请求，
    因此没有 CORS 问题，也不需要浏览器做中转。

【接口】（由 schedule_probe.py 侦察得出）
    GET api.seiue.com/scms/timetable/structure
        ?date=YYYY-MM-DD&reflection_id=<id>&resolve_most_used_timetable_id_by_week=true
    GET api.seiue.com/scms/schcal/events?semester_id=<id>&paginated=0
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

from . import cdp, config
from .models import Lesson, Schedule

log = logging.getLogger(__name__)

PROFILE = config.DATA_DIR / "schedule-profile"
API = "https://api.seiue.com"
WEB = "https://yly.seiue.com"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36 Edg/153.0.0.0")

# 用户指定的晚自习时段
EVENING_START = "18:30"
EVENING_END = "22:00"
EVENING_NAME = "晚自习"


# ==================================================================
# 会话读取（只读 localStorage，不接触密码）
# ==================================================================

TOKEN_FILE = config.DATA_DIR / "schedule_token.json"


def save_token(sess: dict) -> None:
    """把课表会话存下来 —— 以后无需浏览器即可抓课表。"""
    if not sess.get("access_token"):
        return
    try:
        config.ensure_dirs()
        payload = dict(sess)
        payload["saved_at"] = time.time()
        tmp = TOKEN_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(TOKEN_FILE)
        log.info("已保存课表会话 token")
    except Exception as e:
        log.warning("保存课表 token 失败：%s", e)


def load_token() -> dict:
    """读取已保存的课表会话。"""
    try:
        d = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not d.get("access_token"):
        return {}
    # JWT 一般 7~30 天有效；超过 25 天就当作过期
    age = time.time() - float(d.get("saved_at") or 0)
    if age > 25 * 86400:
        log.info("课表 token 已存放 %d 天，可能过期", int(age // 86400))
    return d


def token_age_text() -> str:
    d = load_token()
    if not d:
        return "无"
    age = time.time() - float(d.get("saved_at") or 0)
    return f"{int(age // 3600)} 小时前"


def read_session() -> dict:
    """读取课表网站的登录会话。

    优先用已保存的 token（不需要浏览器）；
    没有时再尝试从运行中的浏览器读取。
    """
    saved = load_token()
    if saved:
        return saved

    if not cdp.is_running(PROFILE):
        #  不提示「请运行某个脚本」—— 由 autologin 自动处理
        return {"error": "课表登录态不可用"}

    port = cdp.read_devtools_port(PROFILE)
    if not port:
        return {"error": "找不到调试端口"}

    conn = cdp.CDP(port)
    try:
        pages = [t for t in conn.targets()
                 if t.get("type") == "page"
                 and "seiue" in (t.get("url") or "").lower()]
        if not pages:
            pages = [t for t in conn.targets() if t.get("type") == "page"]
        if not pages:
            return {"error": "找不到课表标签页"}

        t = pages[0]
        pg = cdp.Page(conn, conn.attach(t["targetId"]), t["targetId"])

        raw = pg.eval("localStorage.getItem('persist:root')")
        if not raw:
            return {"error": "localStorage 无 persist:root（可能未登录）"}

        root = json.loads(raw)
        sess_str = root.get("session")
        if not sess_str:
            return {"error": "会话数据缺失"}
        sess = json.loads(sess_str)

        tok = sess.get("oAuthToken") or {}
        out = {
            "access_token": tok.get("accessToken") or "",
            "reflection_id": str(tok.get("activeReflectionId") or ""),
            "user_name": (sess.get("currentUser") or {}).get("name") or "",
            "semester_id": "",
        }
        # 学期 id 常在页面上下文里，尝试读取
        try:
            sem = pg.eval("""
              (() => {
                try {
                  const s = localStorage.getItem('g-p-semester-selector');
                  return s || '';
                } catch(e) { return ''; }
              })()
            """)
            if sem and sem.isdigit():
                out["semester_id"] = sem
        except Exception:
            pass

        if not out["access_token"]:
            out["error"] = "未找到 accessToken"
        else:
            #  存下来 —— 以后抓课表就不用浏览器了
            save_token(out)
        return out
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    finally:
        conn.close()


# ==================================================================
# HTTP
# ==================================================================

def api_get(path: str, token: str, params: dict | None = None,
            timeout: int = 30) -> tuple[int, str]:
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/plain, */*",
        "User-Agent": UA,
        "Origin": WEB,
        "Referer": WEB + "/",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


# ==================================================================
# 工具
# ==================================================================

def _s(v) -> str:
    """把字段（可能是 dict）压成字符串。"""
    if v is None:
        return ""
    if isinstance(v, dict):
        for k in ("name", "title", "display_name", "label"):
            if v.get(k):
                return str(v[k])
        return ""
    if isinstance(v, (list, tuple)):
        return "、".join(_s(x) for x in v if _s(x))
    return str(v).strip()


def _norm_time(v) -> str:
    """统一成 'YYYY-MM-DDTHH:MM'。

    输入形如 "2026-09-17 08:00:00" 或 "2026-09-17T08:00:00"。
    """
    if v in (None, ""):
        return ""
    if isinstance(v, (int, float)):
        try:
            return datetime.fromtimestamp(v).strftime("%Y-%m-%dT%H:%M")
        except Exception:
            return ""
    s = str(v).strip().replace("/", "-")
    if "T" in s:
        s = s.replace(" ", "T")
    elif " " in s:
        s = s.replace(" ", "T", 1)
    return s[:16]


# ==================================================================
# 解析（真实结构，见 data/schedule_recon/cand_*.json）
# ==================================================================

def parse_events(data) -> list[Lesson]:
    """解析 /chalk/calendar/personals/{id}/events 的响应。

    真实结构（实测）：
        {
          "type": "lesson",
          "start_time": "2026-09-17 08:00:00",
          "end_time":   "2026-09-17 08:40:00",
          "title": "物理",
          "subject": {"name": "物理", "ename": "Physics",
                      "class_name": "物理3班"},
          "address": "E107",
          "remark": "3|4|P1",
          "initiators": [{"name": "..."}]      ← 教师
        }
    """
    rows = data
    if isinstance(data, dict):
        for k in ("data", "results", "items", "events"):
            if isinstance(data.get(k), list):
                rows = data[k]
                break
        else:
            rows = []
    if not isinstance(rows, list):
        return []

    out: list[Lesson] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        st = _norm_time(r.get("start_time"))
        if not st:
            continue

        title = _s(r.get("title"))
        subj = r.get("subject")
        subj_name = _s(subj.get("name")) if isinstance(subj, dict) else _s(subj)
        class_name = ""
        ename = ""
        if isinstance(subj, dict):
            class_name = _s(subj.get("class_name"))
            ename = _s(subj.get("ename"))

        lesson_title = title or subj_name or "(未命名)"

        # 教师
        teacher = ""
        ini = r.get("initiators")
        if isinstance(ini, list) and ini:
            names = [_s(x.get("name")) if isinstance(x, dict) else _s(x)
                     for x in ini]
            teacher = "、".join(n for n in names if n)

        end = _norm_time(r.get("end_time"))
        remark = _s(r.get("remark"))       # 形如 "3|4|P1"
        period = ""
        if remark:
            parts = [p for p in remark.split("|") if p]
            period = parts[-1] if parts else ""

        kind = "正课"
        if r.get("type") and r["type"] != "lesson":
            kind = _s(r["type"])

        out.append(Lesson(subject=lesson_title,
            teacher=teacher,
            room=_s(r.get("address")),
            start=st,
            end=end,
            day=st[:10],
            kind=kind,
            period=period,
            note=class_name or ename,
        ))
    return out


# ==================================================================
# 主入口
# ==================================================================

def fetch_schedule(days_back: int = 1, days_ahead: int = 14) -> Schedule:
    """抓取课表（含晚自习）。

    默认抓「昨天 ~ 未来两周」：
      - 覆盖本周与下一周（用户提醒课表有「下一周」选项）
      - 向前多留一天，便于显示已结束的课
    """
    sess = read_session()
    if sess.get("error"):
        return Schedule(ready=False, error=sess["error"])

    token = sess["access_token"]
    rid = sess["reflection_id"]
    if not token or not rid:
        return Schedule(ready=False, error="缺少 token 或用户标识")

    today = date.today()
    start = (today - timedelta(days=days_back)).isoformat()
    end = (today + timedelta(days=days_ahead)).isoformat()

    status, text = api_get(f"/chalk/calendar/personals/{rid}/events", token,
        {
            "start_time": f"{start} 00:00:00",
            "end_time": f"{end} 23:59:59",
            "expand": "address,initiators",
        },
        timeout=40,
    )
    if status != 200:
        return Schedule(ready=False, error=f"接口 HTTP {status}")

    try:
        data = json.loads(text)
    except Exception:
        return Schedule(ready=False, error="接口返回非 JSON")

    lessons = parse_events(data)

    # 去重
    seen: set[tuple] = set()
    uniq: list[Lesson] = []
    for l in sorted(lessons, key=lambda x: (x.day, x.start)):
        k = (l.day, l.start, l.subject)
        if k in seen:
            continue
        seen.add(k)
        uniq.append(l)

    uniq = add_evening_study(uniq)

    if not uniq:
        return Schedule(ready=False, error="接口返回成功，但没有课程数据")

    return Schedule(source="yly.seiue.com",
        updated=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ready=True,
        lessons=uniq,
        periods=fetch_periods(token, rid),
    )


def fetch_periods(token: str, rid: str) -> list[dict]:
    """抓「节次时间表」—— P1..P9 各自的起止时间。

     用途：课表里**空档也要占一行**（标成「自习」）。
      比如数学是 P2、化学是 P4，那 P3 应该显示一行「自习」，
      而不是让化学直接接在数学后面 —— 那样看不出中间是空的。

      要知道「P3 是几点到几点」，就需要这份节次表。

     这个接口不需要 date 参数（传了也行），返回固定的节次结构。
      抓不到不影响主流程 —— 返回空列表，前端会退回「不填空档」的行为。
    """
    try:
        status, text = api_get("/scms/timetable/structure",
            token,
            {
                "reflection_id": str(rid),
                "resolve_most_used_timetable_id_by_week": "true",
            },
            timeout=20,
        )
        if status != 200:
            log.warning("节次表接口 HTTP %s，跳过空档填充", status)
            return []
        data = json.loads(text)
    except Exception as e:
        log.warning("节次表抓取失败（不影响课表）：%s", e)
        return []

    # 响应结构可能是 list 或 {data: list} / {items: list}
    items = data
    if isinstance(data, dict):
        for k in ("data", "items", "result", "list"):
            v = data.get(k)
            if isinstance(v, list):
                items = v
                break
    if not isinstance(items, list):
        return []

    out: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        name = _s(it.get("name") or it.get("title")
                  or it.get("period") or it.get("label"))
        start = _norm_time(it.get("start_time") or it.get("startTime")
                           or it.get("start"))
        end = _norm_time(it.get("end_time") or it.get("endTime")
                         or it.get("end"))
        if not name or not start:
            continue
        out.append({"period": name, "start": start, "end": end})

    # 按开始时间排序（保证 P1..P9 顺序正确）
    out.sort(key=lambda x: x["start"])
    if out:
        log.info("节次表：%d 节（空档可填充）", len(out))
    return out


def add_evening_study(lessons: list[Lesson]) -> list[Lesson]:
    """为每个上课日补一节晚自习（18:30–22:00，用户指定）。

    用户说明：16:30 放学后，18:30 开始晚自习到 22:00，
    这也算作一节课，需要额外加入。

    判定依据：当天有正课 → 说明是上课日 → 加晚自习。
    """
    have_class = {l.day for l in lessons if l.kind == "正课" and l.day}
    #  只统计「已经是晚自习」的日期（早期版本把每条课的日期都配了晚自习标签，导致不会添加）
    has_evening = {l.day for l in lessons if l.kind == EVENING_NAME and l.day}

    out = list(lessons)
    for day in sorted(have_class - has_evening):
        out.append(Lesson(subject=EVENING_NAME,
            start=f"{day}T{EVENING_START}",
            end=f"{day}T{EVENING_END}",
            day=day,
            kind="晚自习",
        ))
    out.sort(key=lambda l: (l.day, l.start))
    return out
