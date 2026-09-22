"""生成界面预览页（带 mock 数据），用于在浏览器里验证 UI。

背景：pywebview 窗口无法截图，所以做 UI 验证时用浏览器打开
一份「带了假数据」的副本，效果与真实窗口一致。

用法：
    .venv\\Scripts\\python.exe make_preview.py
    → 生成 _preview.html，用浏览器打开即可
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
ROOT = Path(__file__).resolve().parent


def _day(offset: int = 0) -> str:
    """今天 + offset 天的日期（YYYY-MM-DD）。"""
    import datetime as _dt
    return (_dt.date.today() + _dt.timedelta(days=offset)).isoformat()


def _mk_lessons() -> list[dict]:
    """课表 mock —— 日期**相对今天**生成。

    ★ 为什么要动态生成：以前这里写死 2026-09-18 之类的日期，
      预览页过几天就成了「全在过去」的数据，自编课插不进去、
      课表页也看不出效果。改成相对日期后，哪天打开都对。
    """
    rows = [
        # 昨天
        (0, -1, "P2", "08:55", "09:40", "生物", "C-105", "孙老师"),
        # 今天：留出 P3 空档，方便看「自编课自动落位」
        (0, 0, "P1", "08:00", "08:45", "数学", "A-301", "王老师"),
        (0, 0, "P2", "08:55", "09:40", "物理", "实验楼 2", "刘老师"),
        (0, 0, "P4", "10:45", "11:30", "语文", "A-208", "张老师"),
        (0, 0, "P5", "13:00", "13:45", "英语", "B-102", "李老师"),
        (0, 0, None, "18:30", "22:00", "晚自习", "自习室", ""),
        # 明天
        (0, 1, "P1", "08:00", "08:45", "英语", "B-102", "李老师"),
        (0, 1, "P3", "09:50", "10:35", "化学", "实验楼 1", "陈老师"),
        # 后天
        (0, 2, "P2", "08:55", "09:40", "生物", "C-105", "孙老师"),
    ]
    out = []
    for _w, off, per, st, en, subj, room, teacher in rows:
        row = {"day": _day(off), "start": st, "end": en,
               "subject": subj, "room": room, "teacher": teacher}
        if per:
            row["period"] = per
        out.append(row)
    return out


MOCK = {
    "status": "ok",
    "updated": "18:05",
    "message": "",
    "auto_refresh": True,
    "next_refresh_in": 24,
    "courses": [
        {
            "class_id": "ids", "name": "IDS Big History",
            "teacher": "Mr. Zhao", "url": "https://x/managebac/classes/1",
            "overall_level": "A", "overall_percent": 93.5,
            "pending_count": 1, "late_count": 0, "submitted_count": 2,
            "categories": [
                {"name": "Participation", "weight": 30, "percent": 95.0,
                 "level": "A", "graded": True},
                {"name": "Assessments", "weight": 50, "percent": 91.2,
                 "level": "A", "graded": True},
                {"name": "Final Project", "weight": 20, "percent": None,
                 "level": None, "graded": False},
            ],
            "tasks": [
                {"title": "Unit 1 Poster Presentation", "url": "https://x/t/1",
                 "due_date": "2026-09-20", "days_left": 2, "kind": "Homework",
                 "category": "Formative", "score": "A 100 / 100 pts",
                 "level": "A", "percent": 100, "score_num": 100,
                 "score_max": 100, "submitted": False, "graded": True,
                 "pending": False, "completed": True, "late": False,
                 "has_attachment": True, "class_id": "ids",
                 "course": "IDS Big History"},
                {"title": "Sourcing Table Analysis", "url": "https://x/t/2",
                 "due_date": "2026-09-19", "days_left": 1, "kind": "Quiz",
                 "category": "Summative", "score": "N/A", "level": None,
                 "percent": None, "submitted": False, "graded": False,
                 "pending": True, "completed": False, "late": False,
                 "has_attachment": False, "class_id": "ids",
                 "course": "IDS Big History"},
            ],
        },
        {
            "class_id": "phy", "name": "Physics",
            "teacher": "Ms. Liu", "url": "https://x/managebac/classes/2",
            "overall_level": "B", "overall_percent": 84.0,
            "pending_count": 2, "late_count": 1, "submitted_count": 1,
            "categories": [
                {"name": "Lab Reports", "weight": 40, "percent": 88.5,
                 "level": "B", "graded": True},
                {"name": "Unit Tests", "weight": 60, "percent": 81.0,
                 "level": "B", "graded": True},
            ],
            "tasks": [
                {"title": "Pre-Physics_Homework2", "url": "https://x/t/3",
                 "due_date": "2026-09-18", "days_left": 0, "kind": "Homework",
                 "category": "Formative", "score": "", "level": None,
                 "percent": None, "submitted": False, "graded": False,
                 "pending": True, "completed": False, "late": False,
                 "has_attachment": True, "class_id": "phy",
                 "course": "Physics"},
            ],
        },
    ],
    "tasks": [
        {"title": "Unit 1 Poster Presentation", "url": "https://x/t/1",
         "due_date": "2026-09-20", "days_left": 2, "kind": "Homework",
         "category": "Formative", "score": "A 100 / 100 pts",
         "level": "A", "percent": 100, "submitted": False, "graded": True,
         "pending": False, "completed": True, "late": False,
         "has_attachment": True, "class_id": "ids",
         "course": "IDS Big History"},
        {"title": "Sourcing Table Analysis", "url": "https://x/t/2",
         "due_date": "2026-09-19", "days_left": 1, "kind": "Quiz",
         "category": "Summative", "score": "N/A", "submitted": False,
         "graded": False, "pending": True, "completed": False, "late": False,
         "has_attachment": False, "class_id": "ids",
         "course": "IDS Big History"},
        {"title": "Pre-Physics_Homework2", "url": "https://x/t/3",
         "due_date": "2026-09-18", "days_left": 0, "kind": "Homework",
         "category": "Formative", "score": "", "submitted": False,
         "graded": False, "pending": True, "completed": False, "late": False,
         "has_attachment": True, "class_id": "phy", "course": "Physics"},
        {"title": "湖心亭看雪读后感", "url": "https://x/t/4",
         "due_date": "2026-09-15", "days_left": -3, "kind": "Homework",
         "category": "Formative", "score": "", "submitted": False,
         "graded": False, "pending": False, "completed": False, "late": True,
         "has_attachment": False, "class_id": "chn", "course": "语文"},
        {"title": "Bio Lab Safety Quiz", "url": "https://x/t/5",
         "due_date": "2026-09-25", "days_left": 7, "kind": "Quiz",
         "category": "Formative", "score": "", "submitted": False,
         "graded": False, "pending": True, "completed": False, "late": False,
         "has_attachment": False, "class_id": "bio", "course": "Biology"},
    ],
    "schedule": {
        "ready": True,
        #  节次时间表（真实数据里由 app/schedule.py 抓取）。
        #  自编课的「时刻 ↔ P几」联动靠它换算，所以预览也要有。
        "periods": [
            {"period": "P1", "start": "08:00", "end": "08:45"},
            {"period": "P2", "start": "08:55", "end": "09:40"},
            {"period": "P3", "start": "09:50", "end": "10:35"},
            {"period": "P4", "start": "10:45", "end": "11:30"},
            {"period": "P5", "start": "13:00", "end": "13:45"},
            {"period": "P6", "start": "13:55", "end": "14:40"},
            {"period": "P7", "start": "14:50", "end": "15:35"},
            {"period": "P8", "start": "15:45", "end": "16:30"},
        ],
        "lessons": _mk_lessons(),
    },
}

FILES = {
    "ok": True,
    "folderId": "",
    "breadcrumb": [{"name": "根目录", "folderId": ""}],
    "items": [
        {"kind": "folder", "name": "Unit 1 Materials", "folderId": "9001"},
        {"kind": "folder", "name": "Past Papers", "folderId": "9002"},
        {"kind": "file", "name": "Syllabus_2026.pdf", "url": "https://x/f/1",
         "size": "482 KB", "author": "Mr. Zhao", "modified": "2 天前"},
        {"kind": "file", "name": "Lab_Report_Template.docx",
         "url": "https://x/f/2", "size": "24 KB", "author": "Ms. Liu",
         "modified": "5 天前"},
        {"kind": "file", "name": "Grades_Export.xlsx", "url": "https://x/f/3",
         "size": "18 KB", "author": "System", "modified": "1 周前"},
    ],
    "folders": [{"kind": "folder", "name": "Unit 1 Materials",
                 "folderId": "9001"}],
    "files": [{"kind": "file", "name": "Syllabus_2026.pdf",
               "url": "https://x/f/1"}],
    "total": 5,
}

DETAIL = {
    "ok": True,
    "title": "Pre-Physics_Homework2",
    "category": "Formative",
    "kind": "Homework",
    "due_text": "2026-09-18",
    "status": "Pending",
    "description": "请周一早自习将纸质版交给科代表。\\n\\n要求：\\n1. 写出完整解题步骤\\n2. 画受力分析图",
    "attachments": [
        {"url": "https://x/a/1", "name": "Pre-Physics_Homework2.pdf",
         "size": "137.4 KB"},
    ],
    "grade": {"text": "Not Assessed Yet", "level": None, "score": None,
              "max": None, "percent": None},
}

# ── 晚报 mock（含新增作业 / 新增成绩 / 完成 / 到期 / 逾期）──
DIGEST = {
    "ok": True,
    "date": "2026-09-18",
    "weekday": "周五",
    "generated": "2026-09-18 21:00",
    "first_run": False,
    "gpa": {"gpa": 3.52, "percent": 90.35, "letter": "A-", "count": 6},
    "gpa_delta": 0.42,
    "counts": {
        "new_tasks": 3, "new_grades": 2, "new_done": 2,
        "due_soon": 1, "overdue": 2, "pending": 5, "courses": 9,
    },
    "new_tasks": [
        {"title": "Sourcing Table Analysis", "course": "IDS Big History",
         "url": "https://x/t/2", "due_date": "2026-09-19", "kind": "Quiz"},
        {"title": "Pre-Physics_Homework2", "course": "Physics",
         "url": "https://x/t/3", "due_date": "2026-09-18",
         "kind": "Homework"},
        {"title": "《乡土中国》读书笔记", "course": "语文",
         "url": "https://x/t/9", "due_date": "2026-09-23",
         "kind": "Homework"},
    ],
    "new_grades": [
        {"title": "Model Millionaire Quiz", "course": "English Language Arts I",
         "url": "https://x/t/7", "score": "B 82 / 100 pts",
         "level": "B", "percent": 82},
        {"title": "Unit 1 Poster Presentation",
         "course": "IDS Big History", "url": "https://x/t/1",
         "score": "A 100 / 100 pts", "level": "A", "percent": 100},
    ],
    "new_done": [
        {"title": "Materials Check", "course": "English Language Arts I",
         "url": "https://x/t/6", "due_date": "2026-09-16"},
        {"title": "Bio Lab Safety Quiz", "course": "Biology",
         "url": "https://x/t/5", "due_date": "2026-09-17"},
    ],
    "due_soon": [
        {"title": "Pre-Physics_Homework2", "course": "Physics",
         "url": "https://x/t/3", "due_date": "2026-09-18",
         "kind": "Homework"},
    ],
    "overdue": [
        {"title": "湖心亭看雪读后感", "course": "语文",
         "url": "https://x/t/4", "due_date": "2026-09-15", "kind": "Homework"},
        {"title": "Vocab Quiz 4", "course": "English Language Arts I",
         "url": "https://x/t/8", "due_date": "2026-09-16", "kind": "Quiz"},
    ],
    "history": [
        {"date": "2026-09-18", "weekday": "周五",
         "counts": {"new_tasks": 3, "new_grades": 2}},
        {"date": "2026-09-17", "weekday": "周四",
         "counts": {"new_tasks": 1, "new_grades": 0}},
        {"date": "2026-09-16", "weekday": "周三",
         "counts": {"new_tasks": 0, "new_grades": 4}},
    ],
}


def _check_js_quotes(js: str, base_line: int = 1) -> list[str]:
    """挑出跨行的 JS 单双引号字符串 —— 这类问题会让整块脚本失效。

    只检查「引号内出现裸换行」，不含正则/模板字符串分析，
    所以只适合检查纯数据片段（注入的 mock），不适合整份 app.js。
    教训：f-string 里的 \\n 会被解释成真实换行，落到 JS
    单引号字符串里就跨行 → 整块 SyntaxError → window.pywebview
    从未定义 → 面板显示「当前模式不支持」。
    """
    problems: list[str] = []
    i = 0
    line = base_line
    n = len(js)
    while i < n:
        ch = js[i]
        if ch == "\n":
            line += 1
            i += 1
            continue
        if ch in "\"'`":
            quote = ch
            start_line = line
            i += 1
            while i < n:
                c = js[i]
                if c == "\\":
                    i += 2
                    continue
                if c == "\n":
                    if quote != "`":
                        snippet = js[max(0, i - 45): i].replace("\n", " ").strip()
                        problems.append(
                            f"第 {start_line} 行：{quote} 字符串里有裸换行"
                            f"（结尾 …{snippet[-40:]}）"
                        )
                        break
                    line += 1
                if c == quote:
                    i += 1
                    break
                i += 1
            continue
        i += 1
    return problems


def main() -> int:
    src = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    #  拆文件之后，index.html 用 <link> 引 CSS、<script src> 引 JS。
    #   但预览页是「单文件、直接双击打开」的，靠相对路径也能加载 ——
    #   为了稳（避免某些浏览器 file:// 的限制），这里把 CSS 内联回来。
    link_tag = '<link rel="stylesheet" href="app.css">'
    if link_tag in src:
        css_path = ROOT / "web" / "app.css"
        if css_path.exists():
            css = css_path.read_text(encoding="utf-8")
            src = src.replace(link_tag, f"<style>\n{css}\n</style>", 1)

    #  拆文件之后 index.html 用 <link href="app.css"> 引样式。
    #   预览页生成的目录和 web/ 不同（在项目根），所以要改成 web/app.css，
    #   否则预览页会没有样式（白底黑字、圆角全丢）。
    src = src.replace('href="app.css"', 'href="web/app.css"', 1)


    mock_js = f"""
<script>
/* ═══ 预览用 mock：拦截所有 api 调用，喂假数据 ═══ */
const MOCK = {json.dumps(MOCK, ensure_ascii=False)};
const MOCK_FILES = {json.dumps(FILES, ensure_ascii=False)};
const MOCK_DETAIL = {json.dumps(DETAIL, ensure_ascii=False)};
const MOCK_DIGEST = {json.dumps(DIGEST, ensure_ascii=False)};

try {{ localStorage.clear(); }} catch(e) {{}}

window.pywebview = {{
  api: {{
    get_data: () => Promise.resolve(MOCK),
    refresh: () => Promise.resolve({{ok:true}}),
    set_auto_refresh: () => Promise.resolve({{ok:true}}),
    auto_status: () => Promise.resolve({{enabled:true}}),
    /*  引导页：默认「不需要」，加 ?setup=1 就能测引导流程。
       测「测试登录」时：账号里含 "bad" 就模拟失败，否则成功。 */
    setup_status: () => Promise.resolve({{
      need_setup: new URLSearchParams(location.search).has('setup'),
      data_dir: 'C:/Users/me/AppData/Local/ManageBac-packer',
      frozen: true
    }}),
    test_login: (login, password, url) => new Promise(res => setTimeout(() => res(String(login).includes('bad')
        ? {{ok:false, error:'Invalid Email or password'}}
        : {{ok:true, message:'登录成功'}}),
      900)),
    save_setup: () => Promise.resolve({{
      ok:true, path:'C:/Users/me/AppData/Local/ManageBac-packer/credentials.json'
    }}),
    /*  开机自启：预览里模拟一台「支持但还没装」的 Windows。
        用 localStorage 记住开关，这样点一下能看到状态变化。 */
    autostart_status: () => Promise.resolve({{
      installed: localStorage.getItem('mb.preview.autostart') === '1',
      path: 'C:/Users/me/AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup/ManageBac-packer 预热.lnk',
      supported: true
    }}),
    autostart_install: () => {{
      localStorage.setItem('mb.preview.autostart', '1');
      return Promise.resolve({{ok:true, path:'（预览）'}});
    }},
    autostart_uninstall: () => {{
      localStorage.removeItem('mb.preview.autostart');
      return Promise.resolve({{ok:true}});
    }},
    open_data_folder: () => Promise.resolve({{ok:true}}),
    warmup_status: () => Promise.resolve({{
      ok:true, reason:'cache_fresh', message:'开机已预热',
      cache_age: 120, cache_fresh: true
    }}),
    warmup_now: () => Promise.resolve({{ok:true}}),
    minimize: () => Promise.resolve({{ok:true}}),
    open_url: (u) => {{ console.log('open_url', u); return Promise.resolve({{ok:true}}); }},
    show_login_help: () => Promise.resolve({{ok:true}}),
    mark_done: () => Promise.resolve({{ok:true, total:1}}),
    unmark_done: () => Promise.resolve({{ok:true, total:0}}),
    manual_count: () => Promise.resolve({{total:0}}),
    task_detail: () => new Promise(res => setTimeout(() => res(MOCK_DETAIL), 1500)),    /*  故意延迟，模拟真实网络 */
    files_list: () => Promise.resolve(MOCK_FILES),
    files_download: () => Promise.resolve({{ok:true, count:1, total:1, folder:'C:\\\\x'}}),
    files_download_all: () => Promise.resolve({{ok:true, count:3, total:3, folder:'C:\\\\x'}}),
    download_task: () => Promise.resolve({{ok:true, count:1, total:1, folder:'C:\\\\x'}}),
    download_files: () => Promise.resolve({{ok:true, count:3, total:3, folder:'C:\\\\x'}}),
    download_file: () => Promise.resolve({{ok:true, count:1, total:1, folder:'C:\\\\x'}}),
    list_files: () => Promise.resolve(MOCK_FILES),
    open_folder: () => Promise.resolve({{ok:true}}),
    digest_status: () => Promise.resolve({{
      ok:true, unread:true, has_any:true,
      date:'2026-09-18', count:3, hour:21, workday:true
    }}),
    digest_latest: () => Promise.resolve(MOCK_DIGEST),
    digest_by_date: (d) => Promise.resolve({{
      ...MOCK_DIGEST, date: d,
      weekday: '', generated: d + ' 21:00',
      counts: {{new_tasks:1,new_grades:0,new_done:1,
                 due_soon:0,overdue:0,pending:4,courses:9}},
      new_tasks: MOCK_DIGEST.new_tasks.slice(0,1),
      new_grades: [], new_done: MOCK_DIGEST.new_done.slice(0,1),
      due_soon: [], overdue: []
    }}),
    /* Teams / EC */
    teams_status: () => Promise.resolve({{
      ok:true, ready:true, updated:'2026-09-21 08:12:00',
      ec_count:2, post_count:1, attach_count:1, note:''
    }}),
    teams_open: () => Promise.resolve({{ok:true}}),
    teams_sync: (rounds) => Promise.resolve({{
      ok:true, scanned:8, ec:2, posts:1, attachments:1,
      scrolled:true, at_top:(rounds||8)>=25, scroll_rounds:(rounds||8),
      scroll_grew:3, saved_ec:2, saved_posts:1
    }}),
    ec_list: () => Promise.resolve({{ok:true, items:[
      {{kind:'ec', title:'EC roster - Sep 21', date:'2026-09-21',
       channel:'ENGLISH CORNER', author:'Ms. Wang',
       timeLabel:'08:12', text:'Today EC is in B-203.',
       file_text:'English Corner Roster\\n\\nGroup A: Zhang San, Li Si\\n\\nPlease arrive at 12:30.',
       file_note:'1 page', attachments:[{{name:'EC_20260921.pdf'}}]}},
      {{kind:'ec', title:'EC cancelled', date:'2026-09-20',
       channel:'ENGLISH CORNER', author:'Ms. Wang',
       timeLabel:'09:00', text:'EC this Friday is cancelled.', attachments:[]}},
      {{kind:'homework', title:'Unit 2 Essay', date:'2026-09-21',
       channel:'Homework', author:'Mr. Zhao', timeLabel:'10:30',
       text:'Write 500 words.', attachments:[{{name:'rubric.pdf'}}]}}
    ]}}),
    /*  ★ 全部 Teams 消息（跨团队 / 跨频道）——
        预览里给三条，覆盖「有回复」「作业」「EC」三种形态，
        好把分组、回复列表、标签都跑一遍。 */
    tm_all: () => Promise.resolve({{ok:true, total:3, items:[
      {{kind:'homework', title:'Homework - x2 To Scale videos',
       date:'2026-09-11', timeLabel:'9/11 16:26',
       team:'IDS Big History 2026-27', channel:'HOMEWORK',
       author:'Mike Joyce',
       text:"Hi IDS Big History 2026-27,\\nYour next homework is to watch and make notes on two very interesting videos.\\nThe videos are on ManageBac, in Files -> Unit 1",
       replyCount:2, attachments:[],
       replies:[
         {{author:'Jonathan Liu Yuchen', time:'星期五 13:34',
          text:'收到，谢谢老师'}},
         {{author:'Bridges Wang Xuanqi', time:'星期四 19:44',
          text:'看完了'}}
       ]}},
      {{kind:'ec', title:'English Corner roster - Week 3',
       date:'2026-09-14', timeLabel:'9/14 10:46',
       team:'Beijing 101 High School', channel:'ENGLISH CORNER ROSTER',
       author:'Ms. Wang',
       text:'This week EC is in B-203. Group A arrives 12:30.',
       replyCount:0, attachments:[{{name:'EC_week3.pdf'}}],
       file_text:'English Corner Roster\\n\\nGroup A: Zhang San, Li Si\\nGroup B: Wang Wu',
       file_note:'1 页',
       replies:[]}},
      {{kind:'post', title:'House points!',
       date:'2026-09-14', timeLabel:'9/14 10:30',
       team:'Beijing 101 High School', channel:'House points!',
       author:'Zane Hickman',
       text:'Alan Liu (Phoenix), Frank Zhang (Qilin), Mabel Zhang (Thunderbird) - 10 points each.',
       replyCount:0, attachments:[], replies:[]}}
    ]}}),
    tm_stats: () => Promise.resolve({{
      ok:true, at:'2026-09-22 13:38:05',
      nTeams:7, nChannels:17, nPosts:39, nReplies:33, teams:[]
    }}),
    tm_crawl: (n) => Promise.resolve({{
      ok:true, partial:false, nTeams:7, nChannels:17,
      nPosts:39, nReplies:33, at:'2026-09-22 13:38:05', teams:[]
    }}),
    tm_crawl_status: () => Promise.resolve({{
      ok:true, running:false, message:''
    }}),
    tm_channels: () => Promise.resolve({{
      ok:true, at:'2026-09-22 13:38:05', channels:[]
    }}),
    teams_download_ec: () => Promise.resolve({{
      ok:true, downloaded:1, text_extracted:1, total:1, dir:'D:/x/ec'
    }}),
    digest_read: () => Promise.resolve({{ok:true}}),
    backup_status: () => Promise.resolve({{
      ok:true, manual_done:3, digests:5, grade_history:128,
      export_dir:'C:/Users/me/Desktop', max_mb:20, version:1
    }}),
    backup_export: () => Promise.resolve({{
      ok:true, path:'C:/Users/me/Desktop/ManageBac-备份-20260920-1200.json',
      counts:{{manual_done:3,digests:5,grade_history:128}}, bytes:8421
    }}),
    backup_import: () => Promise.resolve({{
      ok:true, manual_done:3, digests:5, grade_history:128,
      backup_before:'D:/x/data/_before_import_x', merged:true
    }}),
    pick_backup_file: () => new Promise(res => setTimeout(() => res('C:/Users/me/Desktop/ManageBac-备份-20260920-1200.json'),
      300)),
  }}
}};
</script>
"""

    # 把 mock 插到主脚本之前
    #
    #  支持两种引用方式（拆文件之后是外链）：
    #     <script src="app.js"></script>
    #     <script>\nconst $ = s => ...      （老的内联写法）
    markers = [
        '<script src="app.js"></script>',
        "<script>\nconst $ = s => document.querySelector(s);",
    ]
    marker = next((mk for mk in markers if mk in src), None)
    if marker is None:
        print("[X] 找不到主脚本入口（app.js 或内联脚本）")
        return 1

    #  mock 必须**排在主脚本之前**，且要用普通 <script> 内联，
    #   不能是 src —— 否则 window.pywebview 还没定义，主脚本就跑了。
    if marker.startswith("<script src="):
        # 外链形式：把 mock 插在它前面，并把外链脚本**内联**进来
        # —— 因为预览页要的是「单文件、可直接 double-click 打开」。
        js_path = ROOT / "web" / "app.js"
        app_js = js_path.read_text(encoding="utf-8")
        out = src.replace(marker,
            mock_js + "<script>\n" + app_js + "\n</script>",
            1,
        )
    else:
        out = src.replace(marker, mock_js + marker, 1)

    dst = ROOT / "_preview.html"
    dst.write_text(out, encoding="utf-8")
    print(f"[OK] 已生成 {dst.name}  ({len(out):,} 字节)")

    # ── 自检：注入的 mock 片段里不能有跨行的单双引号字符串 ──────
    # 这一段是纯数据，检查零误报；出问题会让整块脚本失效，
    # 表现为面板显示「当前模式不支持」。
    bad = _check_js_quotes(mock_js)
    if bad:
        print("")
        print("[!] mock 片段有问题（浏览器里 pywebview 会是 undefined）：")
        for msg in bad:
            print("    " + msg)
        return 1

    print(f"     直接在浏览器打开： {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
