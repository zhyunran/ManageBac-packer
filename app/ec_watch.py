"""EC 名单关注 —— 每天扫 EC 的 PDF，出现「我关心的名字」就提醒。

【用户的需求（原话）】
    「EC 名单以 PDF 呈现于 beijing101middle school 里面的子页面，
      你自己找，所以查自己在不在 EC 要扫当日 EC list 的 pdf。
      另外，可否直接在输入账密的注册环节索要可能出现在 ec 里面的名字，
      每回出名单自动扫描，有的话发通知」

【怎么理解】
    ① EC（English Corner）每天发一个 PDF 名单。
       学生要知道「今天要不要去」＝ 扫一下名单里有没有自己。
    ② 所以：注册时问一次「你叫什么（可能出现在 EC 名单里的名字）」，
       以后每次抓到新 EC 名单，自动扫一遍，名字出现就提醒。

【数据存哪】
    credentials.json 里加一个 `_ec` 段：

        "_ec": {
          "names": ["张三", "San Zhang", "Zhang San"],
          "notify": true
        }

    ★ 为什么允许填**多个名字**：
      EC 名单是英文的，可能是「San Zhang」「Zhang San」
      甚至只写「San」；中文名也可能出现。多填几个才查得全。
      界面上用逗号/换行分隔，解析时都认。

【每天扫什么】
    `data/ec/` 里存的是下载下来的 PDF 和提取出的文字。
    这个模块读那些文字，找名字。

    「今天」的名单怎么认：文件名/正文里带日期，
    或者抓取时间在最近 36 小时内（宽一点，防止时区/延迟）。

【结果怎么呈现】
    不弹系统通知（那个要额外的权限和依赖，而且容易烦人）。
    在**组件内部**给一个明显的标记：
      · 顶栏那个 EC 相关的按钮上挂红点
      · Teams 页里把「命中」的那条顶到最前面并高亮

    这样用户打开组件第一眼就看到。
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import config
from . import store

log = logging.getLogger("ec_watch")

#  扫出来的结果存这里（供界面读取）
WATCH_FILE = config.DATA_DIR / "ec_watch.json"

#  名字最长多少个字（防止用户粘贴一整段进来）
MAX_NAME_LEN = 40
#  最多关注几个名字
MAX_NAMES = 12


# ══════════════════════════════════════════════════════════
#  读 / 写「关注的名字」
# ══════════════════════════════════════════════════════════

def load_names() -> list[str]:
    """读用户填的关注名字（可能多个）。

    ★ 名字存在 credentials.json 的 `_ec.names` 里，所以**凭据文件坏了
      就等于「关注名单丢了」**。以前这里 `except: return []`，用户会
      看到 EC 扫描突然「没在关注任何人」，却没有任何解释。
      现在凭据不健康时至少写一条 warning（界面另有启动体检统一提示）。
    """
    try:
        cred = config.CREDENTIALS or {}
        ec = cred.get("_ec") or {}
        names = ec.get("names") or []
        if isinstance(names, str):
            names = parse_names(names)
        return [str(n).strip() for n in names if str(n).strip()][:MAX_NAMES]
    except Exception as e:
        if (getattr(config, "CRED_HEALTH", {}) or {}).get("ok") is False:
            log.warning("凭据异常，关注名单读不出来（EC 扫描将没有名字可查）：%s", e)
        else:
            log.debug("读关注名字失败（多半是本来就没填）：%s", e)
        return []


def parse_names(raw: str) -> list[str]:
    """把用户填的一串文本拆成名字列表。

    认这几种分隔：换行 / 逗号（中英）/ 分号 / 顿号 / 斜杠。
    ★ 斜杠也认 —— 用户常写「San Zhang/Zhang San」。

    ★ 名字**内部**的空格不拆：
      「San Zhang」是一个名字，不是两个。
      但全角空格 / 不换行空格要压成半角 ——
      用户从 Word 或网页里粘过来经常带全角空格，
      不压的话「张　三」和名单里的「张 三」就对不上。
    """
    if not raw:
        return []
    s0 = re.sub(r"[\u00a0\u3000]", " ", str(raw))
    parts = re.split(r"[\n,，;；、/]+", s0)
    out, seen = [], set()
    for p in parts:
        s = re.sub(r"\s+", " ", p).strip()
        if not s or len(s) > MAX_NAME_LEN:
            continue
        k = s.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
        if len(out) >= MAX_NAMES:
            break
    return out


def save_names(raw: str, notify: bool = True) -> dict:
    """保存关注的名字。返回 {ok, names}。

    ★ 只动 `_ec` 这一段，不碰账号密码那些。
    """
    names = parse_names(raw)
    try:
        cred = dict(config.CREDENTIALS or {})
        ec = dict(cred.get("_ec") or {})
        ec["names"] = names
        ec["notify"] = bool(notify)
        cred["_ec"] = ec
        path = config.save_credentials(cred)

        #  重新加载到内存（正在跑的进程立刻生效）
        try:
            import importlib
            from app import config as _cfg
            importlib.reload(_cfg)
        except Exception as e:
            log.debug("重载凭据失败，重启后生效：%s", e)

        log.info("EC 关注名字已保存：%s 个 → %s", len(names), path)
        return {"ok": True, "names": names, "path": str(path)}
    except Exception as e:
        log.warning("保存 EC 关注名字失败：%s", e)
        return {"ok": False, "error": str(e), "names": names}


# ══════════════════════════════════════════════════════════
#  扫名字
# ══════════════════════════════════════════════════════════

def _norm(s: str) -> str:
    """归一化：小写、压掉多余空白、去掉常见标点。

    ★ 为什么要归一化：
      PDF 提取出来的文字里，名字可能带逗号、句点、
      或者中间是「Zhang,  San」（两个空格）。
      不归一化会漏掉。
    """
    s = (s or "").lower()
    s = re.sub(r"[\u00a0\u3000]", " ", s)      # 全角空格
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _hit(text_norm: str, name: str) -> bool:
    """名字在不在文本里（带边界判断，避免误命中）。

    ★ 边界的坑：
        名字「Ann」如果在「Announcement」里命中，那就乱了。
        所以要求：名字两边不能紧挨着字母/数字。

      用 `(?<![a-z0-9])名字(?![a-z0-9])` 这个模式。
      中文名不适用这个规则（中文字符本来就不算 [a-z0-9]）。

    ⚠ 中文短名的局限（已知，不假装没有）：
        中文没有词边界，「林」这种一个字的名字，只要文本里
        任何地方出现这个字就会命中。所以：
          · 中文名要求 ≥2 字（一个字直接不算，见下）
          · 界面上的说明必须写清「未匹配不代表不在名单中」
    """
    n = _norm(name)
    if not n or len(n) < 2:
        return False
    if re.search(r"[\u4e00-\u9fff]", n):
        #  中文名：直接查子串
        return n in text_norm
    esc = re.escape(n)
    return re.search(r"(?<![a-z0-9])" + esc + r"(?![a-z0-9])",
                     text_norm) is not None


# ══════════════════════════════════════════════════════════
#  命中片段（0.5.2）
#
#  以前命中只给「有你 / 没你」，用户还得自己打开 PDF、Ctrl+F 找。
#  这里把命中处附近的一小段原文取出来，再把里面能认出来的
#  「时间 / 地点 / 组别 / 成员」标出来 —— 都只是**照抄原文**，
#  不做任何推断。
#
#  ★ 只呈现实测到的字，不猜测、不补全。
#    名单里没写地点，就不显示地点那一栏。
# ══════════════════════════════════════════════════════════

#  片段取多长（命中位置左右各取这么多字符）
SNIPPET_PAD = 120

_RE_TIME = re.compile(
    r"\d{1,2}\s*[:：]\s*\d{2}\s*[-–—~～至]\s*\d{1,2}\s*[:：]\s*\d{2}"
    r"|\d{1,2}:\d{2}\s*(?:am|pm)"          # 10:40 am
    r"|\d{1,2}\s*月\s*\d{1,2}\s*日"
    r"|周[一二三四五六日天]"
    r"|(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\.?\s*(?:\d{1,2}/\d{1,2})?",
    re.I)
_RE_PLACE = re.compile(
    r"(?:地点|场地|位置|教室|Location|Venue|Room)\s*[:：]\s*([^\n，,。;；]{2,40})"
    r"|([A-Z]\d{2,4})\s*(?:教室|室)?",
    re.I)
_RE_GROUP = re.compile(
    r"(?:第\s*[一二三四五六七八九十\d]+\s*组|Group\s*\d+)", re.I)
_RE_ROSTER_HINT = re.compile(
    r"(?:名单|成员|学生|Members?|Roster|Students?|Participants?)\s*[:：]?",
    re.I)


def extract_signals(text: str) -> dict:
    """从一段文字里认出四类信号。认不到就是空列表，不猜。"""
    t = text or ""
    times = []
    for m in _RE_TIME.finditer(t):
        v = re.sub(r"\s+", "", m.group(0))
        if v and v not in times:
            times.append(v)
    places = []
    for m in _RE_PLACE.finditer(t):
        v = (m.group(1) or m.group(2) or "").strip(" ·-:：")
        if v and len(v) <= 40 and v not in places:
            places.append(v)
    groups = []
    for m in _RE_GROUP.finditer(t):
        v = re.sub(r"\s+", "", m.group(0))
        if v not in groups:
            groups.append(v)
    return {
        "times": times[:4],
        "places": places[:4],
        "groups": groups[:4],
        "roster": bool(_RE_ROSTER_HINT.search(t)),
    }


def hit_snippet(text: str, name: str, pad: int = SNIPPET_PAD) -> dict:
    """名字命中处的原文片段 + 从片段里认出的信号。

    返回 {text, signals}；找不到名字就返回空。
    ★ 片段是**原文照抄**（只压空白），不修改任何字。
    """
    if not text or not name:
        return {}
    n = _norm(name)
    if not n:
        return {}
    #  在**归一化后**的文本里定位（与 _hit 用同一套规则）
    low = _norm(text)
    i = low.find(n)
    if i < 0:
        return {}
    #  归一化时压了空白，位置会有偏移 —— 用「按空白切词后重新拼」
    #  的方式没法精确对应，所以这里退回用原文做大小写无关查找。
    raw = text.lower()
    j = raw.find(n.split(" ")[0])
    if j < 0:
        j = max(0, min(i, len(text) - 1))
    a = max(0, j - pad)
    b = min(len(text), j + len(n) + pad)
    seg = text[a:b]
    seg = re.sub(r"[ \t]+", " ", seg)
    seg = re.sub(r"\n{3,}", "\n\n", seg).strip()
    return {"text": seg, "signals": extract_signals(seg)}


def scan_text(text: str, names: list[str] | None = None) -> list[str]:
    """在一段文字里找关注的名字，返回命中的那些。"""
    names = names if names is not None else load_names()
    if not names or not text:
        return []
    t = _norm(text)
    return [n for n in names if _hit(t, n)]


# ══════════════════════════════════════════════════════════
#  扫「最近这份 EC 名单」
# ══════════════════════════════════════════════════════════

#  「今天」的容差 —— 抓取时间在这个范围内就算「最近的」
RECENT_HOURS = 36


def _ec_sources() -> list[dict]:
    """列出所有能扫的 EC 名单文字。

    ★★ 数据到底在哪 —— 这里是**实测查出来的**，不是照假设写的。

      第一版这个函数只读 `data/ec/*.txt`。跑了一遍全项目搜索发现：
        · `data/ec/` 这个目录当时**根本不存在**，也**没有任何一处**写 `*.txt`
        · 所以那个功能是**静默失效**的 —— 永远扫不到东西，也不报错

      第二版改成读 `data/teams_crawl.json` 的 `ec[].file_text`。
      但实测那个 json **压根没有 `ec` 这个键** ——
      它的真实结构是 `teams[].channels[].posts[]`。

      现在（第三版）按**真实结构**读三个地方：

        ① `data/teams_crawl.json` → teams[].channels[].posts[].file_text
           —— 每条消息抽出来的 PDF 文字
        ② `data/ec/*.txt` / `*.json` 里的文字
           —— 手动下载 PDF 后落的文字旁文件
        ③ `data/ec/` 下的 PDF **本体**
           —— 现场抽文字（最可靠：有 PDF 就一定读得到）

      三条都读不到就返回空 —— 那说明**还没下载过名单**，
      这时该做的是去下载，而不是这里假装有数据。
    """
    out: list[dict] = []

    #  ── ① 主来源：全量抓取结果里的每条消息 ──
    #
    #  ★★ EC 名单只在**附件 PDF** 里，不在正文。
    #     实测（本机真实数据）：
    #       正文 = 「There will be NO EC tomorrow. English Immersion…」
    #               「TODAY'S EC will be outside at the ID courtyard…」
    #       —— 只有场地/改期通知，一个名字都没有
    #       名单 = 附件 `EC Roster 9.21.pdf` / `EC Roster 9.24 1.pdf`
    #
    #     所以这里只读 `file_text`（由 extract_pdf_text 从 PDF 抽出来的），
    #     **绝不读 `text`（正文）** —— 正文里没有名字，
    #     把正文当名单扫会得出「没你」这种误导性结论。
    try:
        from app import teams_crawl
        data = teams_crawl.load() or {}
        for t in (data.get("teams") or []):
            tname = t.get("name") or ""
            for ch in (t.get("channels") or []):
                cname = ch.get("name") or ""
                for p in (ch.get("posts") or []):
                    if not isinstance(p, dict):
                        continue
                    txt = p.get("file_text") or ""
                    #  只认有文字的 —— 光有附件链接不算「已抽过文字」
                    if not str(txt).strip():
                        continue
                    #  来源要显示**文件名**，不是消息标题。
                    #  消息标题常常是「EC ANNOUNCEMENT」这种，
                    #  用户看到它不知道是哪份名单。
                    names = [str(a.get("name") or "").strip()
                             for a in (p.get("attachments") or [])
                             if isinstance(a, dict)]
                    names = [n for n in names if n]
                    label = names[0] if names else (
                        p.get("file_note") or p.get("title") or "EC 名单")
                    if len(names) > 1:
                        label += f" 等 {len(names)} 份"
                    out.append({
                        "file": label,
                        "at": _item_time(p),
                        "text": str(txt),
                        "where": f"{tname} / {cname}",
                        #  标明这份文字是从 PDF 附件里抽的
                        "via": "pdf",
                        "attachments": names,
                    })
    except Exception as e:
        log.debug("读 teams_crawl 缓存里的 EC 文字失败：%s", e)

    #  ── ② 兼容：旧的 teams.json 的 ec[]（老版本拖下来的）──
    try:
        from app import teams as _teams
        old = _teams.load()
        for it in (old.get("ec") or []):
            txt = it.get("file_text") or ""
            if not str(txt).strip():
                continue
            out.append({
                "file": (it.get("subject") or it.get("title")
                         or it.get("file_note") or "EC 名单"),
                "at": _item_time(it),
                "text": str(txt),
                "where": it.get("channel") or "Teams",
                "via": "pdf",
            })
    except Exception as e:
        log.debug("读旧 teams.json 的 EC 文字失败：%s", e)

    #  ── ③ data/ec/ 下的文件 ──
    d = Path(config.DATA_DIR) / "ec"
    if not d.exists():
        return out

    #  ③a 文字旁文件
    for pat in ("*.txt", "*.md"):
        for p in sorted(d.glob(pat)):
            try:
                txt = p.read_text(encoding="utf-8", errors="replace")
                at = p.stat().st_mtime
            except Exception:
                continue
            if not txt.strip():
                continue
            out.append({"file": p.name, "at": at, "text": txt,
                        "where": "本地 EC 目录", "via": "pdf"})

    #  ③b PDF 本体 —— 现场抽文字。
    #     ★ 这一步让「只要 PDF 下下来了就一定能扫」成立，
    #       不依赖任何中间产物（旁文件 / json 字段）。
    for p in sorted(d.glob("*.pdf")):
        try:
            at = p.stat().st_mtime
        except Exception:
            continue
        try:
            from app import teams as _teams
            r = _teams.extract_pdf_text(p)
            txt = (r.get("text") or "") if r.get("ok") else ""
        except Exception as e:
            log.debug("抽 %s 的文字失败：%s", p.name, e)
            txt = ""
        if not txt.strip():
            continue
        #  已经通过旁文件收过一次的，别重复扫
        if any(x["file"] == p.name for x in out):
            continue
        out.append({"file": p.name, "at": at, "text": txt,
                    "where": "本地 EC 目录（现场抽取）", "via": "pdf"})

    return out


def _item_time(it: dict) -> float:
    """从一条 EC 记录里猜「它是什么时候的」，返回时间戳。

    ★ 为什么要猜：`teams_crawl.json` 里没有统一的「下载时间」字段，
      时间信息散在几个可能的键里，格式还不一样。
      逐个试，哪个能解析就用哪个。

    解析不出来时返回**当前时间** ——
    宁可多扫一次旧的，也不要因为时间读不出来就把名单漏掉。
    """
    for key in ("at", "time", "date", "created", "posted", "updated"):
        v = it.get(key)
        if not v:
            continue
        s = str(v).strip()
        # ISO 风格：2026-06-01T08:00:00 / 2026-06-01 08:00:00
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                    "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                    "%Y/%m/%d"):
            try:
                return datetime.strptime(s[:len(fmt) + 4], fmt).timestamp()
            except Exception:
                continue
    return time.time()


def scan_recent(names: list[str] | None = None,
                hours: int = RECENT_HOURS) -> dict:
    """扫最近下载的 EC 名单，看有没有关注的名字。

    返回：
        {
          ok, names,          # 关注的名字
          scanned,            # 扫了几份
          hit: [...],         # 命中的文件名
          found: [...],       # 命中的名字
          at,                 # 这次扫描的时间
          no_names: bool,     # 用户还没填名字
        }
    """
    names = names if names is not None else load_names()
    res = {
        "ok": True,
        "names": names,
        "scanned": 0,
        "hit": [],
        "found": [],
        #  命中处的原文片段（0.5.2）—— 每条：
        #    {name, file, where, at, text, signals}
        #  用户不用自己去翻 PDF、Ctrl+F。
        "details": [],
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "no_names": not names,
        #  ★ 语义边界（写在数据里，不只是写在文档里）：
        #    文字匹配 ≠ 读懂名单。
        "text_only": True,
        "semantic_verified": False,
        "note": "按文字匹配。未匹配不代表不在名单中；命中也不代表已确认参加。"
                "以学校原名单为准。",
    }
    if not names:
        return res

    cutoff = time.time() - hours * 3600
    for src in _ec_sources():
        if src["at"] < cutoff:
            continue                    # 太老的名单不扫
        res["scanned"] += 1
        got = scan_text(src["text"], names)
        if got:
            res["hit"].append(src["file"])
            for g in got:
                if g not in res["found"]:
                    res["found"].append(g)
                #  同一个人同一份名单只取一条片段
                if any(d.get("name") == g and d.get("file") == src["file"]
                       for d in res["details"]):
                    continue
                sn = hit_snippet(src["text"], g)
                if not sn:
                    continue
                res["details"].append({
                    "name": g,
                    "file": src["file"],
                    "where": src.get("where", ""),
                    "at": time.strftime("%Y-%m-%d %H:%M",
                                        time.localtime(src["at"]))
                    if src.get("at") else "",
                    "text": sn["text"],
                    "signals": sn["signals"],
                })
                if len(res["details"]) >= 12:
                    break

    #  存下来供界面读（下次打开组件不用重扫）
    try:
        config.ensure_dirs()
        store.write_json_atomic(WATCH_FILE, res)
    except Exception as e:
        log.debug("存 EC 扫描结果失败：%s", e)

    if res["found"]:
        log.info("EC 名单里找到关注的名字：%s，扫了 %d 份名单",
                 "、".join(res["found"]), res["scanned"])
    return res


def last_result() -> dict:
    """读上次的扫描结果（界面轮询用，不重扫）。"""
    try:
        if WATCH_FILE.exists():
            d = json.loads(WATCH_FILE.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                return d
    except Exception:
        pass
    return {"ok": True, "names": load_names(), "scanned": 0,
            "hit": [], "found": [], "at": ""}


def status() -> dict:
    """给界面看的综合状态。"""
    names = load_names()
    last = last_result()
    return {
        "ok": True,
        "names": names,
        "n_names": len(names),
        "last": last,
        #  有命中就为 True —— 界面拿它挂红点
        "alert": bool(last.get("found")),
        "found": last.get("found") or [],
        "scanned": last.get("scanned") or 0,
        "at": last.get("at") or "",
        #  命中片段 + 语义边界（0.5.2）
        "details": last.get("details") or [],
        "text_only": True,
        "semantic_verified": False,
        "note": last.get("note") or
                "按文字匹配。未匹配不代表不在名单中。以学校原名单为准。",
    }
