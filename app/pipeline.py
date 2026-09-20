"""数据管道：抓取 → 计算 → 缓存。

所有抓取在**后台线程**执行；UI 只读内存快照，保证界面秒开。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from . import cdp, config, digest, httpclient, manual_done, store
from .lock import scrape_lock
from .models import Course, Schedule, Task
from .scraper import Scraper, summarize_courses

log = logging.getLogger(__name__)

# ==================== 取数方式 ====================
# "http"  —— 纯 HTTP 请求（默认；几秒内、稳定、开机预热无负担）
# "browser" —— 通过 CDP 驱动真实 Edge（仅在 HTTP 失效时的退路）
FETCH_MODE = "http"

_lock = threading.Lock()
_state: dict = {
    "status": "idle",        # idle | running | ok | need_login | error
    "message": "",
    "updated": "",
    "tasks": [],
    "courses": [],
    "summary": {},
    "schedule": Schedule().to_dict(),
    # ---- 自动刷新状态 ----
    "auto_refresh": True,
    "refreshing": False,
    "last_run_ts": 0.0,        # 上次抓取完成的时刻
    "next_run_ts": 0.0,        # 下次计划抓取的时刻
}


def snapshot() -> dict:
    """返回当前数据 + 实时计算的倒计时。

    注意：返回值必须是**纯 JSON 可序列化**的（pywebview 传给 JS 时要序列化），
    因此这里把 Task / Course 对象转成 dict。
    """
    now = time.time()
    with _lock:
        auto = _state["auto_refresh"]
        nxt = _state["next_run_ts"]
        if auto and nxt > now:
            remain = int(round(nxt - now))
        else:
            remain = 0

        tasks = [
            t if isinstance(t, dict) else t.to_dict() for t in _state["tasks"]
        ]
        courses = [
            c if isinstance(c, dict) else c.to_dict() for c in _state["courses"]
        ]

        return {
            "status": _state["status"],
            "message": _state["message"],
            "updated": _state["updated"],
            "tasks": tasks,
            "courses": courses,
            "summary": dict(_state["summary"]),
            "schedule": dict(_state["schedule"]),
            "auto_refresh": auto,
            "interval": config.AUTO_REFRESH_SEC,
            "interval_text": config.AUTO_REFRESH_TEXT,
            "next_refresh_in": remain,
            "refreshing": _state["refreshing"],
            "login_error": _state.get("login_error", ""),
            "server_time": datetime.now().isoformat(timespec="seconds"),
            #  给首次运行引导页用：学校网址（默认值）
            #   前端用它预填输入框，用户大多不用改
            "host": _host_of(config.BASE_URL),
        }


def _set(**kw) -> None:
    with _lock:
        _state.update(kw)


def _host_of(url: str) -> str:
    """从完整 URL 里取出域名部分（给引导页预填用）。

    'https://your-school.managebac.cn/student/home'
      → 'your-school.managebac.cn'
    """
    u = (url or "").strip()
    if not u:
        return ""
    u = u.split("://", 1)[-1]          # 去掉协议
    return u.split("/", 1)[0]          # 去掉路径


def _friendly_login_msg(raw: str) -> str:
    """把技术性的登录失败原因，翻译成用户能看懂的话。

     用户要求「集中处理，不要说让我自己跑某个脚本」。
      所以这里绝不再提「请运行 xxx.py」，改成说明现象 + 说明已经自动做了什么。
    """
    raw = raw or ""
    if any(k in raw for k in ("锁定", "锁")):
        return ("账号被学校系统临时锁定了（通常是密码输错几次导致）。\n"
                "稍等一会儿会自动恢复，程序会自己重试，你不用管。")
    if any(k in raw for k in ("不正确", "密码", "credential")):
        return ("账号或密码不正确。\n"
                "请检查项目里的 credentials.json 是否填对（注意学校域名容易写错）。")
    if "风控" in raw or "webdriver" in raw:
        return "浏览器指纹被识别了，正在用另一种方式重试……"
    if "超时" in raw or "timeout" in raw.lower():
        return "登录超时了（网络慢或学校网站忙）。稍后会自动重试。"
    if not raw:
        return "登录状态过期，正在自动重新登录……"
    return f"自动重新登录未成功：{raw}\n稍后会自动再试。"


# ------------------------------------------------- 手动完成标记

def apply_manual_marks() -> int:
    """把手动完成标记应用到**当前内存数据**（不重新抓取）。

    用户点「我已完成，不再提示」后立即调用 —— 让任务当场从待办消失，
    不必等下一轮 30 秒刷新。
    """
    with _lock:
        raw = list(_state["tasks"])
    if not raw:
        return 0

    objs = [t if isinstance(t, Task) else Task(**t) for t in raw]
    n = manual_done.apply(objs)

    # 同步到「课程明细」里的同名任务（两处显示必须一致）
    by_key: dict[str, Task] = {}
    for t in objs:
        k = manual_done.key_for(t.url, t.title, t.course)
        if k:
            by_key[k] = t

    with _lock:
        _state["tasks"] = objs
        for c in _state["courses"]:
            if not isinstance(c, Course):
                continue
            for ct in c.tasks:
                k = manual_done.key_for(ct.url, ct.title, ct.course)
                hit = by_key.get(k)
                if hit is not None:
                    ct.manual = hit.manual
                    ct.completed = hit.completed
    return n


def save_cache_now() -> None:
    """把当前内存状态立刻写入缓存（手动标记后持久化）。"""
    with _lock:
        payload = {
            "updated": _state["updated"],
            "tasks": [t if isinstance(t, dict) else t.to_dict()
                      for t in _state["tasks"]],
            "courses": [c if isinstance(c, dict) else c.to_dict()
                        for c in _state["courses"]],
            "summary": dict(_state["summary"]),
            "schedule": dict(_state["schedule"]),
        }
    try:
        store.save_cache(payload)
    except Exception as e:
        log.warning("写入手动标记后的缓存失败：%s", e)


# ---------------------------------------------------------------- 缓存

def load_from_cache() -> bool:
    cached = store.load_cache()
    if not cached:
        return False
    try:
        tasks = [Task(**t) for t in cached.get("tasks", [])]
        courses: list[Course] = []
        for raw in cached.get("courses", []):
            d = dict(raw)
            task_dicts = d.pop("tasks", [])
            course = Course(**d)
            course.tasks = [Task(**t) for t in task_dicts]
            courses.append(course)
        _set(tasks=tasks, courses=courses,
            updated=cached.get("updated", ""),
            summary=cached.get("summary", {}),
            schedule=cached.get("schedule", Schedule().to_dict()),
            status="ok" if (tasks or courses) else "idle",
            message="",
        )
        return True
    except Exception as e:
        log.warning("缓存解析失败：%s", e)
        return False


# ---------------------------------------------------------------- 抓取

def _run_refresh() -> None:
    """执行一次完整抓取。

    用文件锁保证同一时刻只有一个进程在驱动 Edge
    （桌面小组件与网页版看板可能同时开着）。
    抢不到锁时改为直接读缓存，避免两边互抢导致失败。
    """
    with scrape_lock(config.DATA_DIR / "scrape.lock") as got:
        if not got:
            log.info("另一个进程正在抓取，本次改为读取缓存")
            load_from_cache()
            _set(status="ok", message="另一窗口正在抓取，已显示最近数据")
            return
        _run_refresh_locked()


def _retry(fn, label: str, tries: int = 3, default=None, delay: float = 1.2):
    """带重试地执行 fn（应对页面偶发未加载完）。

    全部失败时返回 default（默认 None），不抛异常打断整轮抓取。
    """
    import time as _t

    last: Exception | None = None
    for i in range(1, tries + 1):
        # 冷却中就别重试了 —— 重试只会继续 429
        if httpclient.cooldown_remaining() > 0:
            log.info("%s：检测到限速冷却，停止重试", label)
            break
        try:
            r = fn()
            if r:                    # 非空视为成功
                return r
            last = RuntimeError(f"{label} 返回空")
        except Exception as e:
            last = e
        if i < tries:
            log.info("%s 第 %d 次失败（%s），重试……", label, i, last)
            _t.sleep(delay * i)
    if last is not None and httpclient.cooldown_remaining() <= 0:
        log.warning("%s 重试 %d 次仍未成功：%s", label, tries, last)
    return default


def _fetch_courses_parallel(courses: list[Course], workers: int = 3) -> list[Task]:
    """并行抓取多门课程（仅 HTTP 模式）。

    每门课需要 3 次请求（主页 / core_tasks / units），串行 9 门课要 27 次
    往返。这里用线程池并行，把总耗时从约 60 秒压到约 15 秒。

     workers 保守设为 3 —— 并发太高会触发站点限速（429）。
    每个线程有**自己的** HttpSession（各自的 Cookie jar），
    因此不存在线程安全问题。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    log.info("并行抓取 %d 门课程（%d 线程）……", len(courses), workers)

    def one(course: Course) -> list[Task]:
        """抓一门课。失败重试一次（网络抖动时很常见）。

         每次重试都用**全新**的 HttpSession —— 因为 scraper 会把结果
          写进 course 对象，复用可能残留半截数据。

        注意：部分课程**本来就没有 core task**（例如 Politics、
        IDS Big History）—— 返回 0 属于正常，这里不打 WARNING，
        只记 DEBUG，避免日志里出现误导性的「失败」。
        """
        last: Exception | None = None
        for attempt in (1, 2):
            # 已经进冷却了就别再开新会话 —— 只会继续 429
            if httpclient.cooldown_remaining() > 0:
                log.debug("%s：限速冷却中，跳过", course.name)
                return []
            s = httpclient.HttpSession(auto_login_on_demand=False)
            try:
                s.open()
                if s.rate_limited:
                    return []              # 冷却标记已置，交给上层收尾
                sc = Scraper(s)
                probe = Course(name=course.name, url=course.url,
                               class_id=course.class_id)
                sc.fetch_course_detail(probe)
                if probe.tasks:
                    course.tasks = probe.tasks
                    course.teacher = probe.teacher or course.teacher
                    course.categories = probe.categories or course.categories
                    if probe.overall_level:
                        course.overall_level = probe.overall_level
                    if probe.overall_percent is not None:
                        course.overall_percent = probe.overall_percent
                    return course.tasks
                last = RuntimeError("返回 0 个任务")
            except Exception as e:
                last = e
            finally:
                try:
                    s.close()
                except Exception:
                    pass
            if attempt == 1 and httpclient.cooldown_remaining() <= 0:
                log.debug("%s 首次返回空，重试……", course.name)
                time.sleep(1.5)

        log.debug("%s 最终 0 个任务（该课可能本就没有 core task）", course.name)
        return []

    tasks: list[Task] = []
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(one, c): c for c in courses}
        for fut in as_completed(futures):
            c = futures[fut]
            try:
                got = fut.result()
            except Exception as e:
                log.warning("%s 线程异常：%s", c.name, e)
                got = []
            tasks.extend(got)
            done += 1
            _set(message=f"已读取 {done}/{len(courses)} 门课程"
                         f"（共 {len(tasks)} 个任务）……")

    log.info("并行抓取完成：%d 个任务", len(tasks))
    return tasks


def detect_attachments(tasks: list[Task], max_probe: int = 40,
                       workers: int = 3) -> int:
    """确认哪些任务有附件（决定是否显示下载键）。

    【策略】三层判断，由便宜到昂贵：
        1. 抓取时卡片里已看出附件 → `has_attachment=True`，直接用
        2. 之前探测过 → 读 `data/attach_probe.json` 缓存（一天有效）
        3. 都没线索 → 抓详情页确认（**只做一次**，之后长期缓存）

     用户要求「只给有附件的任务显示下载键」，
      所以宁可漏判也不能误判 —— 没确认的一律不显示。
    """
    import json as _json
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from . import taskdetail

    cache_file = config.DATA_DIR / "attach_probe.json"
    try:
        cache: dict = _json.loads(cache_file.read_text(encoding="utf-8"))
        if not isinstance(cache, dict):
            cache = {}
    except Exception:
        cache = {}

    # 冷却中不做额外的详情请求
    if httpclient.cooldown_remaining() > 0:
        # 只用已知信息
        for t in tasks:
            key = manual_done.key_for(t.url, t.title, t.course)
            if key in cache:
                t.has_attachment = bool(cache[key])
        return sum(1 for t in tasks if t.has_attachment)

    # 找出需要确认的
    need: list[Task] = []
    for t in tasks:
        if t.has_attachment:
            continue                      # 卡片已确认
        key = manual_done.key_for(t.url, t.title, t.course)
        if key and key in cache:
            t.has_attachment = bool(cache[key])
            continue
        need.append(t)

    if not need:
        return sum(1 for t in tasks if t.has_attachment)

    # 未完成的任务优先（已完成的很少需要下载）
    need.sort(key=lambda x: (x.completed, x.days_left if x.days_left is not None
                             else 9999))
    probe = need[:max_probe]
    log.info("附件探测：需确认 %d 个，本轮抓 %d 个", len(need), len(probe))

    def one(t: Task) -> tuple[str, bool]:
        key = manual_done.key_for(t.url, t.title, t.course)
        try:
            d = taskdetail.fetch(t.url)
            if not d.get("ok"):
                return key, bool(t.has_attachment)
            return key, bool(d.get("attachments"))
        except Exception:
            return key, bool(t.has_attachment)

    changed = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(one, t): t for t in probe}
        for fut in as_completed(futs):
            t = futs[fut]
            try:
                key, has = fut.result()
            except Exception:
                continue
            if key:
                cache[key] = has
            if has and not t.has_attachment:
                changed += 1
            t.has_attachment = has

    # 写回缓存
    try:
        config.ensure_dirs()
        tmp = cache_file.with_suffix(".json.tmp")
        tmp.write_text(_json.dumps(cache, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(cache_file)
    except Exception as e:
        log.warning("写附件探测缓存失败：%s", e)

    return sum(1 for t in tasks if t.has_attachment)


def _make_session():
    """按 FETCH_MODE 创建取数会话。

    http    —— 纯 HTTP（默认）。会话过期会**自动重登一次**。
    browser —— CDP 驱动真实 Edge。
    """
    if FETCH_MODE == "browser":
        if not cdp.is_running(config.BROWSER_PROFILE_DIR):
            try:
                cdp.launch(config.BROWSER_PROFILE_DIR, headless=True,
                           url=f"{config.BASE_URL}/student/home")
                cdp.wait_for_port(config.BROWSER_PROFILE_DIR, timeout=45)
            except Exception as e:
                raise RuntimeError(f"浏览器启动失败：{e}") from e
        return cdp.Session(headless=True)

    # 纯 HTTP
    return httpclient.HttpSession(auto_login_on_demand=True)


def _run_refresh_locked() -> None:
    _set(status="running", message="正在抓取数据……")
    config.ensure_dirs()

    #  冷却期内直接跳过 —— 不启动浏览器、不发请求、不重试。
    #   否则会一直 429 → 补抓 → 更久 → 看起来像「卡死」。
    cd = httpclient.cooldown_remaining()
    if cd > 0:
        load_from_cache()
        _set(status="ok",
             message=f"站点限速冷却中（还剩 {int(cd)} 秒），显示上次数据")
        _set(next_run_ts=time.time() + min(cd + 5, 300))
        return

    session = None
    try:
        session = _make_session()
        session.open()

        page = session.page()
        page.goto(f"{config.BASE_URL}/student/home")
        page.wait_for_timeout(1500)

        # HTTP 模式用 check_login()；浏览器模式用 looks_logged_in()
        if hasattr(session, "check_login"):
            logged = session.check_login()
        else:
            logged = cdp.looks_logged_in(page)

        if not logged:
            err = getattr(session, "last_error", "") or ""
            limited = getattr(session, "rate_limited", False)
            if limited:
                # 被限速 —— 保留旧数据，等冷却结束再自动重试
                load_from_cache()
                cd = httpclient.cooldown_remaining()
                _set(status="ok",
                     message=f"站点限速冷却中（还剩 {int(cd)} 秒），显示上次数据")
                _set(next_run_ts=time.time() + min(cd + 5, 300))
                return

            # ══════════════════════════════════════════════════════
            #  会话失效 → **自动重新登录**，不让用户自己跑脚本。
            #   （用户要求：「要么就弹出网页让我登录，最好是你自己解决」）
            #   安全约束：autologin 内部有指数退避 + 锁号识别，
            #   绝不会短时间内反复提交表单。
            # ══════════════════════════════════════════════════════
            _set(status="running", message="登录状态已过期，正在自动重新登录……")
            log.info("检测到未登录，尝试自动重新登录")
            try:
                from . import autologin

                ok, msg = autologin.renew_managebac()
            except Exception as e:
                ok, msg = False, f"{type(e).__name__}: {e}"
                log.warning("自动重新登录异常：%s", e)

            if ok:
                log.info("自动重新登录成功，继续抓取")
                # 关掉旧会话，用新的 Cookie 重开一个
                try:
                    session.close()
                except Exception:
                    pass
                session = _make_session()
                session.open()
                if hasattr(session, "check_login"):
                    logged = session.check_login()
                if logged:
                    _set(message="已自动重新登录，正在读取课程列表……")
                    # 继续往下走（不 return）
                else:
                    _set(status="error",
                         message="自动登录后仍然读取不到数据，请稍后重试")
                    return
            else:
                # 自动登录失败 → 给用户一个「点一下就能登」的入口
                friendly = _friendly_login_msg(msg)
                _set(status="need_login", message=friendly, login_error=msg)
                return

        if not logged:
            return

        scraper = Scraper(session)
        _set(message="正在读取课程列表……")
        courses = _retry(lambda: scraper.fetch_courses(), "课程列表", tries=3)
        log.info("课程 %d 门", len(courses))

        if not courses:
            # 抓不到课程通常是页面还没加载完 / 登录态异常 —— 保留旧缓存
            load_from_cache()
            _set(status="error",
                 message="未读取到课程列表（可能是页面未加载完）。已保留上次数据，稍后会自动重试。")
            return

        all_tasks: list[Task] = []
        if FETCH_MODE == "http" and len(courses) > 1:
            # ---- 并行抓取（HTTP 模式）：9 门课同时抓，约 8 倍提速 ----
            all_tasks = _fetch_courses_parallel(courses)
        else:
            for i, c in enumerate(courses, 1):
                _set(message=f"正在读取 {c.name[:28]}（{i}/{len(courses)}）……")
                _retry(lambda cc=c: scraper.fetch_course_detail(cc), c.name, tries=2,
                       default=None)
                all_tasks.extend(c.tasks)

        if not all_tasks:
            # 完全抓不到 → 很可能被限速；标记冷却，避免马上又重试
            if httpclient.cooldown_remaining() <= 0:
                httpclient.set_cooldown(60, "抓取返回 0 任务")
            load_from_cache()
            _set(status="ok",
                 message="未读取到任务，已显示上次数据，稍后会自动重试。")
            return

        #  完整性校验：若某门课这次 0 任务、但上轮有任务，说明是网络抖动
        #   → 补抓。若上轮也是 0，说明这门课本来就没有任务（正常，
        #   例如「Politics」「Big History」这类课确实可能没有 core task）。
        prev_counts: dict[str, int] = {}
        cached = store.load_cache() or {}
        for c in cached.get("courses", []) or []:
            prev_counts[c.get("name", "")] = len(c.get("tasks", []) or [])

        suspect = [c for c in courses
                   if not c.tasks and prev_counts.get(c.name, 0) > 0]

        #  限速导致的「大面积变空」→ 别浪费时间补抓，直接保留旧数据
        cd = httpclient.cooldown_remaining()
        if cd > 0 and len(suspect) * 2 > len(courses):
            log.info("限速冷却中（还剩 %.0f 秒），放弃本轮，保留旧数据", cd)
            load_from_cache()
            _set(status="ok",
                 message=f"站点限速冷却中（还剩 {int(cd)} 秒），显示上次数据")
            _set(next_run_ts=time.time() + min(cd + 5, 300))
            return

        if suspect:
            log.info("%d 门课程上轮有任务、本轮为空，补抓：%s",
                     len(suspect), ", ".join(c.name for c in suspect))
            _set(message=f"补抓 {len(suspect)} 门未完成的课程……")
            for c in suspect:
                _retry(lambda cc=c: scraper.fetch_course_detail(cc), c.name,
                       tries=2, default=None)
                if c.tasks:
                    all_tasks.extend(c.tasks)

            still = [c for c in suspect if not c.tasks]
            if still:
                names = ", ".join(c.name for c in still)
                log.warning("补抓后仍为空：%s", names)
                # 失败比例过半 → 整轮不可靠，保留旧数据
                if len(still) * 2 > len(courses):
                    load_from_cache()
                    _set(status="error",
                         message=f"{len(still)}/{len(courses)} 门课程未抓到，"
                                 f"已保留上次数据，稍后会自动重试。")
                    return
                _set(message=f"注意：{names} 未取到，其余已更新")

        scraper.save_report()
        summary = summarize_courses(courses,
                                    getattr(scraper, "official_gpa", None))

        #  应用「我已完成，不再提示」的手动标记
        n_manual = manual_done.apply(all_tasks)
        if n_manual:
            log.info("手动标记命中 %d 个任务（已从待办移除）", n_manual)

        #  探测哪些任务有附件（决定是否显示下载键）
        #   卡片里若已能看出附件就直接用；看不出的按需抓详情页确认。
        #   为避免请求过多，只对「卡片没标出」且「未完成」的任务抽样探测。
        try:
            n_att = detect_attachments(all_tasks)
            if n_att:
                log.info("%d 个任务确认有附件（显示下载键）", n_att)
        except Exception as e:
            log.warning("附件探测失败（不影响主流程）：%s", e)

        # 课表：独立抓取（失败不影响成绩数据）
        schedule = _fetch_schedule()

        payload = {
            "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "tasks": [t.to_dict() for t in all_tasks],
            "courses": [c.to_dict() for c in courses],
            "summary": summary,
            "schedule": schedule.to_dict(),
        }
        store.save_cache(payload)
        try:
            store.record_snapshot(summary.get("mean_percent"), {
                "courses": len(courses), "tasks": len(all_tasks)})
        except Exception as e:
            log.warning("写入历史库失败：%s", e)

        #  成绩历史（修复：以前 record_grades 定义了但从未被调用，
        #   所以 grades 表一直是空的）
        try:
            n_g = store.record_grades(courses)
            if n_g:
                log.info("成绩历史已更新 %d 条", n_g)
        except Exception as e:
            log.warning("写入成绩历史失败：%s", e)

        msg = f"已更新：{len(all_tasks)} 个任务 / {len(courses)} 门课程"
        if schedule.ready:
            msg += f" / 课表 {len(schedule.lessons)} 节"
        _set(tasks=all_tasks, courses=courses, summary=summary,
            schedule=schedule.to_dict(),
            updated=payload["updated"], status="ok", message=msg,
        )
        log.info("抓取完成")

        # ── 晚报（用户要求）──
        #  每次抓取完成后：
        #    ① 先试着生成「今天的晚报」（仅工作日 21:00 后、且今天还没生成过）
        #       —— 必须在存新快照**之前**做，否则 diff 会变成空
        #    ② 再把本次数据存为「上次快照」，供下次 diff 用
        try:
            snapshot_data = {
                "tasks": [t.to_dict() for t in all_tasks],
                "courses": [c.to_dict() for c in courses],
                "status": "ok",
            }
            made = digest.ensure_today(snapshot_data)
            if made:
                log.info("晚报已生成：%s", made.get("date"))
            digest.save_snapshot(snapshot_data)
        except Exception as e:
            log.warning("晚报生成失败（不影响主流程）：%s", e)

        # ── 详情页预取（用户要求「点开详情要快」）──
        #  在**后台**把待办任务的详情先抓好，用户点开就是读缓存（<5ms）。
        #   用 start_async：不阻塞本轮抓取，且 10 分钟内不重复。
        #   串行 + 间隔，不会因为预取触发站点限速。
        try:
            from . import prefetch

            if prefetch.start_async(all_tasks):
                log.info("已在后台开始预取任务详情")
        except Exception as e:
            log.warning("启动预取失败（不影响主流程）：%s", e)
    except Exception as e:
        log.exception("抓取失败")
        _set(status="error", message=f"抓取失败：{e}")
    finally:
        try:
            session.close()
        except Exception:
            pass


def refresh_async() -> None:
    if _state["status"] == "running":
        return
    _set(next_run_ts=time.time() + config.AUTO_REFRESH_SEC)   # 手动刷新后重置周期
    threading.Thread(target=_run_refresh, daemon=True).start()


def refresh_sync() -> None:
    _run_refresh()


# ==================== 课表 ====================

def _fetch_schedule() -> Schedule:
    """抓取课表。**token 失效会先自动续期**，用户不需要做任何事。

    用户要求：「不要出现无法打开课表、让我自己运行某个 python 程序这种」。
    所以这里的策略是：
        1. 直接抓  → 成功就完事
        2. 失败且看起来是「token 失效」→ 自动登录续期 → 再抓一次
        3. 续期也失败 → 返回友好提示（不说「请运行 xxx.py」）
    """
    from .schedule import fetch_schedule

    try:
        sch = fetch_schedule()
    except Exception as e:
        log.warning("课表抓取异常：%s", e)
        return Schedule(ready=False, error=f"{type(e).__name__}: {e}")

    if sch.ready:
        return sch

    # ── 失败：判断是不是会话问题 ──
    err = sch.error or ""
    session_issue = any(k in err for k in ("token", "会话", "401", "403", "登录", "缺少", "未保存",
    ))
    if not session_issue:
        return sch

    log.info("课表抓取失败（%s），尝试自动续期登录态……", err)
    try:
        from . import autologin

        ok, msg = autologin.renew_schedule()
    except Exception as e:
        log.warning("课表自动续期异常：%s", e)
        return Schedule(ready=False, error=f"自动续期失败：{e}")

    if not ok:
        log.warning("课表自动续期未成功：%s", msg)
        # 翻译成人话，绝不出现「请运行 xxx.py」
        if "时间" in msg or "分钟" in msg:
            hint = "课表登录态正在恢复，稍后自动重试"
        elif any(k in msg for k in ("没有", "缺")):
            hint = "课表账号未配置（不影响成绩功能）"
        else:
            hint = "课表登录态暂时不可用，稍后自动重试"
        return Schedule(ready=False, error=hint)

    log.info("课表登录态已自动续期，重新抓取")
    try:
        return fetch_schedule()
    except Exception as e:
        log.warning("续期后抓取课表仍失败：%s", e)
        return Schedule(ready=False, error=f"{type(e).__name__}: {e}")


def refresh_schedule_only() -> None:
    """只刷新课表（后台线程）。"""
    def _work() -> None:
        sch = _fetch_schedule()
        _set(schedule=sch.to_dict())
    threading.Thread(target=_work, daemon=True).start()


# ==================== 自动刷新 ====================

_auto_started = False
_auto_thread: threading.Thread | None = None
_wake = threading.Event()


def set_auto_refresh(enabled: bool) -> None:
    """开启 / 关闭自动刷新。"""
    enabled = bool(enabled)
    if enabled:
        # 重新开启时，安排一次近期的刷新
        _set(auto_refresh=True, next_run_ts=time.time() + config.AUTO_REFRESH_SEC)
    else:
        _set(auto_refresh=False, next_run_ts=0.0)


def start_auto_refresh() -> None:
    """启动后台自动刷新线程（每 config.AUTO_REFRESH_SEC 秒抓一次）。"""
    global _auto_started, _auto_thread
    if _auto_started:
        return
    _auto_started = True
    _set(next_run_ts=time.time() + config.AUTO_REFRESH_SEC)
    _auto_thread = threading.Thread(target=_auto_loop, daemon=True)
    _auto_thread.start()
    log.info("自动刷新已启动：每 %d 秒一次", config.AUTO_REFRESH_SEC)


def schedule_next(delay: float | None = None) -> None:
    """手动安排下次刷新时刻（例如刚手动刷新完）。"""
    d = config.AUTO_REFRESH_SEC if delay is None else delay
    _set(next_run_ts=time.time() + d)


def auto_status() -> dict:
    s = snapshot()
    return {
        "auto_refresh": s["auto_refresh"],
        "interval": s["interval"],
        "next_refresh_in": s["next_refresh_in"],
        "refreshing": s["refreshing"],
    }


def _auto_loop() -> None:
    """后台循环：按计划时刻触发抓取；失败则指数退避。"""
    backoff = 0

    while True:
        if not _state["auto_refresh"]:
            _set(next_run_ts=0.0)
            time.sleep(1.0)
            continue

        interval = config.AUTO_REFRESH_SEC + backoff
        # 计划下次执行时刻（snapshot() 据此实时算出剩余秒数）
        _set(next_run_ts=time.time() + interval)

        # 到点前每秒醒来一次，便于快速响应开关变化
        while time.time() < _state["next_run_ts"]:
            if not _state["auto_refresh"]:
                break
            time.sleep(0.5)

        if not _state["auto_refresh"]:
            continue

        # 上一轮还在跑 → 直接进入下一轮
        if _state["status"] == "running":
            continue

        try:
            _set(refreshing=True, next_run_ts=0.0)
            _run_refresh()
        except Exception:
            log.exception("自动刷新异常")
        finally:
            _set(refreshing=False)

        status = _state.get("status")
        if status == "ok":
            backoff = 0
        else:
            base = 60 if status == "need_login" else config.AUTO_REFRESH_SEC
            backoff = min(max(backoff * 2, base), config.AUTO_REFRESH_MAX_BACKOFF)
            log.info("上轮状态=%s，下次延迟 %d 秒", status, backoff)
