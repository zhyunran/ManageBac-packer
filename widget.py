"""ManageBac 桌面小组件 —— 入口。

启动后：
  1. 立刻显示上次缓存（秒开）
  2. 后台自动刷新
  3. 点击任意卡片 → 在你日常使用的 Edge 中打开对应网页

用法：
    uv run widget.py
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent))


import webview  # noqa: E402

from app import config, pipeline  # noqa: E402

# ---------------- 日志 ----------------
config.ensure_dirs()
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.FileHandler(config.LOG_DIR / "widget.log", encoding="utf-8")],
)
log = logging.getLogger("mb")

# pywebview 在 Windows 上读取键盘修饰键状态时有个已知递归 bug，
# 每当窗口获得焦点就会刷一大段 "maximum recursion depth exceeded" 噪音。
# 它是无害的（不影响任何功能），这里把它压掉，保持日志可读。
class _QuietPywebviewNoise(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        if "maximum recursion depth exceeded" in msg:
            return False
        if "Error while processing window.native" in msg:
            return False
        return True


logging.getLogger("pywebview").addFilter(_QuietPywebviewNoise())

TITLE = "ManageBac-packer"
W, H = 470, 780


def _check_js_api_safe(api_obj: object) -> None:
    """ 启动自检：确认 js_api 对象没有「公开的非函数属性」。

    pywebview 注入 JS API 时会遍历 js_api 的**所有公开属性**
    （`webview/util.py` 的 `get_functions`），遇到非函数对象就**递归进去**。
    一旦指向 pywebview 自己的对象（Window / DOM / EventContainer / .NET 窗体），
    递归会爆炸（实测 60 秒），期间 `pywebviewready` / `loaded` 事件全被堵住，
    窗口就一直「未响应」。

    所以在这里挡一道：发现问题就写明显日志，避免又变成用户看到的「未响应」。
    """
    try:
        import inspect as _inspect

        bad = []
        for name in dir(api_obj):
            if name.startswith("_"):
                continue
            attr = getattr(api_obj, name, None)
            if _inspect.ismethod(attr) or _inspect.isfunction(attr):
                continue
            bad.append(f"{name}={type(attr).__name__}")
        if bad:
            log.error(" js_api 自检失败：发现公开的非函数属性 [%s]。"
                "pywebview 会递归遍历它们 —— 极可能导致窗口「未响应」。"
                "请把这些成员改成下划线开头（如 _window）。",
                ", ".join(bad),
            )
        else:
            log.info("js_api 自检通过")
    except Exception as e:
        log.warning("js_api 自检异常：%s", e)


class Api:
    """暴露给前端 JS 的接口。

     铁律：只允许「方法」公开；任何数据成员必须以 `_` 开头。
      原因见 `_check_js_api_safe` 的说明。
    """

    def __init__(self) -> None:
        #  名字必须以下划线开头！
        #   pywebview 注入 JS API 时会遍历本对象的所有「公开」属性，
        #   遇到非函数对象就递归进去（util.py: get_functions）。
        #   `self.window` 指向整个 pywebview Window（含 DOM / EventContainer /
        #   .NET 窗体，且 Window.width/height 是会 wait(15s) 的属性 getter）
        #   → 递归爆炸，实测要 60 秒，期间 pywebviewready / loaded 事件
        #   全被堵住，窗口就一直「未响应」。
        #   改成 `_window` 后 0.00 秒（下划线开头的属性会被跳过）。
        self._window = None
        #  前端报到标志（见 win_ready 的说明）
        self._front_ready = False
        self._warming = False
        self._served = False          # 前端是否已经成功拿到过数据
        self._relogging = False       # 是否正在自动重新登录
        self._teams_syncing = False    # Teams 是否正在同步

    # ---- 数据 ----
    def get_data(self):
        # 第一次被前端调用 = 界面真的活过来了（用于排查「未响应」）
        if not self._served:
            self._served = True
            log.info("界面已就绪（前端首次请求数据）")
        return pipeline.snapshot()

    def refresh(self):
        pipeline.refresh_async()
        return {"ok": True}

    def set_auto_refresh(self, enabled=True):
        pipeline.set_auto_refresh(bool(enabled))
        return {"ok": True, "enabled": bool(enabled)}

    def auto_status(self):
        return pipeline.auto_status()

    # ══════════ 首次运行引导（ 打包版的关键：填账密即用） ══════════
    def setup_status(self):
        """前端问「需要引导吗？」+ 已填过的值（用来回填）。

         只看本地有没有填过账号，不发网络请求 ——
          这样界面一打开就能立刻决定显示引导页还是主界面。

         ★ 顺带把**已经存过的账号**带回去：
           引导页现在分成三页（学校 / 课表 / EC），
           用户可能只是漏了某一页，重新打开时不该让他把
           已经填过的再打一遍。密码**不回传** —— 没必要，
           也不该在界面上暴露。
        """
        cred = config.CREDENTIALS or {}
        mb = cred.get("_managebac") or {}
        sch = cred.get("_schedule") or {}
        try:
            from app import ec_watch
            ec_names = ec_watch.load_names()
        except Exception:
            ec_names = []
        return {
            "need_setup": not config.has_credentials(),
            "data_dir": str(config.ROOT),
            "frozen": bool(getattr(sys, "frozen", False)),
            #  ── 回填用（只有账号，没有密码）──
            "mb_user": str(mb.get("login") or ""),
            "mb_host": str(mb.get("url") or ""),
            "sch_user": str(sch.get("login") or ""),
            "has_schedule": bool(sch.get("login") and sch.get("password")),
            "ec_names": ec_names,
        }

    def save_schedule(self, login: str, password: str):
        """单独保存课表账号（引导页第 2 页）。

         ★ 为什么单独开一个方法，不复用 save_setup：
           引导页分成了三页，课表是**第二页**才填的。
           这时学校的账号已经存好了 —— 再调 save_setup 会把
           第一页的内容重写一遍（虽然值一样，但等于多一次写盘，
           而且万一用户回头改了第一页又没重新提交就麻烦了）。
           这里只动 `_schedule` 那一段，别的一律不碰。

         课表是**另一个网站**（希悦校园），账号形态跟学校系统不同
         （手机号），所以单独存、也不做「必须填」的强制。
        """
        login = (login or "").strip()
        password = (password or "").strip()
        if not login or not password:
            return {"ok": False, "error": "手机号和密码都要填"}
        try:
            #  ★★ 以**磁盘上的文件**为准来合并，不用内存里那份。
            #
            #    原因：引导页现在分两步写盘 ——
            #      第 1 页 save_setup 写学校账号
            #      第 2 页 save_schedule 写课表账号
            #    两次之间如果内存那份是旧的（或另一个进程改过文件），
            #    拿内存去合就会把前一步的内容冲掉。
            #    文件才是唯一的事实来源，每一步都从它读。
            cred = config._load_credentials() or {}
            sc = dict(cred.get("_schedule") or {})
            sc["login"] = login
            sc["password"] = password
            sc.setdefault("url", "https://yly.seiue.com/")
            sc.setdefault("note", "课表系统（yly.seiue.com），用手机号登录")
            cred["_schedule"] = sc
            config.save_credentials(cred)
            #  让内存那份跟上（别让后续调用读到旧的）
            config.CREDENTIALS = cred
            log.info("课表账号已保存：%s", login)
            return {"ok": True}
        except Exception as e:
            log.exception("保存课表账号失败")
            return {"ok": False, "error": f"保存失败：{e}"}

    def save_setup(self, login: str, password: str, url: str = "",
                   schedule_login: str = "", schedule_password: str = "",
                   ec_names: str = ""):
        """保存账号密码（引导页提交）。

         只写「真正需要的键」，不覆盖已有的其他配置。
         写完立刻用新凭据试一次登录，让用户当场知道对不对 ——
          不然他以为填好了，其实要等半小时后才发现是错的。

         ec_names 是**可选**的：用户填「可能出现在 EC 名单里的名字」，
           以后每次抓到 EC 名单自动扫一遍。空着也能用，只是没提醒。
        """
        login = (login or "").strip()
        password = (password or "").strip()
        if not login or not password:
            return {"ok": False, "error": "账号和密码都要填"}

        url = (url or "").strip()
        if url and not url.startswith("http"):
            return {"ok": False, "error": "网址要以 http:// 或 https:// 开头"}

        # 读现有的（保留 _schedule 等其他配置）
        cred = dict(config.CREDENTIALS or {})

        mb = dict(cred.get("_managebac") or {})
        mb["login"] = login
        mb["password"] = password
        if url:
            mb["url"] = url
        mb.setdefault("note", "ManageBac 学生账号（学校系统）")
        cred["_managebac"] = mb

        # 课表账号可选 —— 有些学校没有希悦，不填也能用
        sl = (schedule_login or "").strip()
        sp = (schedule_password or "").strip()
        if sl and sp:
            sc = dict(cred.get("_schedule") or {})
            sc["login"] = sl
            sc["password"] = sp
            sc.setdefault("note", "课表系统（可选）")
            cred["_schedule"] = sc

        # EC 关注名字（可选）—— 存 _ec 段，不和账号混在一起
        try:
            from app import ec_watch
            names = ec_watch.parse_names(ec_names or "")
            if names:
                ec = dict(cred.get("_ec") or {})
                ec["names"] = names
                ec.setdefault("notify", True)
                cred["_ec"] = ec
                log.info("EC 关注名字：%s", "、".join(names))
        except Exception as e:
            log.debug("解析 EC 名字失败，不影响保存：%s", e)

        try:
            path = config.save_credentials(cred)
        except Exception as e:
            log.exception("保存凭据失败")
            return {"ok": False, "error": f"保存失败：{e}"}

        # 重新加载到内存（正在跑的进程也要能立刻用上新账号）
        try:
            import importlib
            from app import config as _cfg
            importlib.reload(_cfg)
            from app import httpclient as _hc
            _hc.CREDENTIALS = _cfg.CREDENTIALS
            _hc.CRED_FILE = _cfg.ROOT / "credentials.json"
        except Exception as e:
            log.warning("重载凭据失败，重启后生效：%s", e)

        log.info("凭据已保存到 %s", path)
        return {"ok": True, "path": str(path)}

    # ══════════ EC 名单关注（用户要求「每回出名单自动扫描」）══════════
    def ec_watch_status(self):
        """给界面看的 EC 关注状态：填了哪些名字、最近有没有命中。"""
        try:
            from app import ec_watch
            return {"ok": True, **ec_watch.status()}
        except Exception as e:
            log.debug("读 EC 关注状态失败：%s", e)
            return {"ok": False, "error": str(e), "names": [],
                    "alert": False, "found": []}

    def ec_watch_save(self, names, notify=True):
        """保存/修改 EC 关注的名字（设置里也能改）。"""
        try:
            from app import ec_watch
            r = ec_watch.save_names(names if isinstance(names, str)
                                    else "、".join(names or []),
                                    bool(notify))
            if r.get("ok"):
                #  存完立刻扫一次 —— 用户填完就想知道「现在我中了吗」
                try:
                    ec_watch.scan_recent()
                except Exception:
                    pass
            return r
        except Exception as e:
            log.debug("保存 EC 名字失败：%s", e)
            return {"ok": False, "error": str(e)}

    def ec_watch_scan(self):
        """手动触发一次扫描（设置里的「立刻检查」）。"""
        try:
            from app import ec_watch
            ec_watch.scan_recent()
            return {"ok": True, **ec_watch.status()}
        except Exception as e:
            log.debug("扫 EC 失败：%s", e)
            return {"ok": False, "error": str(e)}

    # ══════════ 开机自启（打包版「装好即用」的最后一步）══════════
    def autostart_status(self):
        """开机自启装没装。"""
        return autostart_status()

    def autostart_install(self):
        """装开机自启（在「启动」文件夹放一个指向 exe 的快捷方式）。"""
        return autostart_install()

    def autostart_uninstall(self):
        """卸掉开机自启。"""
        return autostart_uninstall()

    def test_login(self, login: str, password: str, url: str = ""):
        """用**用户刚填的**凭据试登录一次。

         不写入任何文件 —— 先验证再保存，避免填错了留在磁盘上。
         绝不重试（失败一次就返回）—— 连续失败会锁号。
        """
        login = (login or "").strip()
        password = (password or "").strip()
        if not login or not password:
            return {"ok": False, "error": "账号和密码都要填"}

        try:
            from app import httpclient

            base = (url or "").strip() or config.BASE_URL
            if not base.startswith("http"):
                base = "https://" + base.lstrip("/")

            # 用独立会话试，不影响正在跑的
            s = httpclient.HttpSession(auto_login_on_demand=False)
            s.base = base.rstrip("/")
            ok, msg = s.login(login, password)
            if ok:
                log.info("引导页测试登录成功")
                return {"ok": True, "message": "登录成功"}
            log.warning("引导页测试登录失败：%s", msg)
            return {"ok": False, "error": msg or "登录失败"}
        except Exception as e:
            log.warning("引导页测试登录异常：%s", e)
            return {"ok": False, "error": f"连接不上：{e}"}

    def open_data_folder(self):
        """打开数据目录（用户想找缓存/日志时用得上）。"""
        try:
            config.ensure_dirs()
            os.startfile(str(config.ROOT))       # noqa: S606 (Windows)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---- 预热：自动登录 + 抓取（让「打开即有内容」）----
    def warmup_status(self):
        """返回预热结果，供界面显示「数据是否是自动抓来的」。"""
        from app import warmup
        st = warmup.read_status()
        st["cache_age"] = int(warmup.cache_age())
        st["cache_fresh"] = warmup.cache_is_fresh()
        return st

    def warmup_now(self):
        """界面上的「立刻预热」按钮：后台自动登录 + 抓取。"""
        from app import warmup
        if getattr(self, "_warming", False):
            return {"ok": True, "already": True}

        self._warming = True

        def work() -> None:
            try:
                warmup.ensure_fresh(verbose=False, force=True)
            except Exception as e:
                log.warning("预热失败：%s", e)
            finally:
                self._warming = False

        threading.Thread(target=work, daemon=True).start()
        return {"ok": True}

    # ══════════ Teams / EC（English Corner）══════════
    #
    # ★ 说明：这块用的是「浏览器会话模式」——
    #   启动组件自带的 Edge，你在里面正常登录 Teams，
    #   程序读页面上**已渲染**的消息。不导出任何令牌。
    #
    # ★ 为什么不做成自动同步
    #   Teams 需要用户先登录一次，这是绕不过去的人工步骤。
    #   所以设计成「你点一下同步」而不是「后台偷偷跑」。
    def teams_status(self):
        """Teams 面板的状态（有没有同步过、有多少条）。"""
        try:
            from app import teams
            return teams.status()
        except Exception as e:
            log.warning("Teams 状态读取失败：%s", e)
            return {"ok": False, "ready": False, "note": str(e),
                    "ec_count": 0, "post_count": 0, "attach_count": 0,
                    "updated": ""}

    def teams_open(self):
        """打开 Teams 窗口让你登录（只做一次）。"""
        try:
            from app import teams_reader
            ok = teams_reader.open_teams(headless=False)
            return {"ok": bool(ok)}
        except Exception as e:
            log.warning("打开 Teams 失败：%s", e)
            return {"ok": False, "error": str(e)}

    #  ── Teams 操作互斥闸 ──
    #  全量收取 / 单页同步 / EC 名单下载 **共用同一个浏览器页面**。
    #  原来三道各自的本操作闸门（_tm_crawling 等）只防自己重入，
    #  互不认识 —— 实测踩中：全量收取正在翻频道时点了「同步」，
    #  同步读到加载到一半的页面，报「没有识别到消息」。
    #  所以加一道「谁在占用」的总闸：后来者拿到明白话，而不是误报。
    _TEAMS_OP_NAMES = {"crawl": "全量收取", "sync": "同步", "ec": "EC 名单下载"}

    def _teams_busy(self, me: str):
        """别的 Teams 操作正在跑 → 返回给用户看的提示；空闲 → None。"""
        who = getattr(self, "_teams_op", None)
        if who and who != me:
            name = self._TEAMS_OP_NAMES.get(who, who)
            return (f"正在{name}，等它跑完再试"
                    f"（这几个功能共用同一个 Teams 页面，同时跑会互相踩）")
        return None

    def teams_sync(self, rounds=8):
        """同步一次（往回翻 → 读页面 → 分类 → 存本地）。

        rounds 是往回翻的轮数：
          8  = 默认，翻到 Teams 不再加载为止（通常够用）
          25 = 「多翻一点」，往更深处翻（会慢一些）

        ★ 放在后台线程里跑，界面不会卡住。
        """
        if getattr(self, "_teams_syncing", False):
            return {"ok": True, "already": True}
        busy = self._teams_busy("sync")
        if busy:
            return {"ok": False, "busy": True, "reason": busy}
        self._teams_syncing = True
        self._teams_op = "sync"

        try:
            n = max(1, min(int(rounds or 8), 40))
        except Exception:
            n = 8
        #  轮数越多，给的总时间也放大，避免「多翻一点」被时间上限截断
        budget = 90 if n <= 10 else min(240, 60 + n * 6)

        box: dict = {}

        def work() -> None:
            try:
                from app import teams_reader
                box.update(teams_reader.sync(scroll_rounds=n, budget_sec=budget))
            except Exception as e:
                log.warning("Teams 同步失败：%s", e)
                box.update({"ok": False, "reason": str(e)})
            finally:
                self._teams_syncing = False
                self._teams_op = None

        t = threading.Thread(target=work, daemon=True)
        t.start()
        t.join(timeout=max(150, budget + 60))
        return box or {"ok": False, "reason": "同步超时"}

    def ec_list(self, keyword="", date=""):
        """EC 列表（可按关键词 / 日期筛）。"""
        try:
            from app import teams
            return {"ok": True, "items": teams.ec_list(keyword or "", date or "")}
        except Exception as e:
            return {"ok": False, "items": [], "error": str(e)}

    def tm_all(self, keyword="", date="", channel="", team=""):
        """**全部** Teams 消息（所有团队 / 所有频道）。

        Teams 标签页用这个 —— 数据来自 `teams_crawl` 的全量抓取结果，
        每条都带 `team` / `channel` 两个来源字段，便于分组显示。

        优先读全量缓存；没有的话退回旧的单频道缓存（兼容老数据）。
        """
        try:
            from app import teams_crawl
            items = teams_crawl.all_messages(keyword or "", date or "",
                                             channel or "", team or "")
            if items:
                return {"ok": True, "items": items, "total": len(items),
                        "source": "crawl"}
        except Exception as e:
            log.warning("读全量 Teams 消息失败：%s", e)

        try:
            from app import teams
            items = teams.all_messages(keyword or "", date or "",
                                       channel or "")
            return {"ok": True, "items": items, "total": len(items),
                    "source": "single"}
        except Exception as e:
            log.warning("读取 Teams 消息失败：%s", e)
            return {"ok": False, "items": [], "total": 0, "error": str(e)}

    def tm_stats(self):
        """Teams 全量数据的概览（团队 / 频道 / 帖子数 / 抓取时间）。"""
        try:
            from app import teams_crawl
            return {"ok": True, **teams_crawl.stats()}
        except Exception as e:
            log.warning("读 Teams 概览失败：%s", e)
            return {"ok": False, "at": "", "nTeams": 0, "nChannels": 0,
                    "nPosts": 0, "nReplies": 0, "teams": [], "error": str(e)}

    def tm_crawl(self, scroll=10):
        """全量抓取：所有团队 → 所有频道 → 消息。

        这个函数**不需要你在 Teams 里点任何东西** ——
        程序自己进每个团队收集频道，再用深度链接逐个频道取数。

        17 个频道实测约 280 秒，所以放后台线程跑，界面不卡。
        """
        if getattr(self, "_tm_crawling", False):
            return {"ok": True, "already": True}
        busy = self._teams_busy("crawl")
        if busy:
            return {"ok": False, "busy": True, "reason": busy}
        self._tm_crawling = True
        self._teams_op = "crawl"

        try:
            n = max(1, min(int(scroll or 10), 30))
        except Exception:
            n = 10

        box: dict = {}

        def progress(msg: str) -> None:
            self._tm_progress = msg            # 供 tm_crawl_status 读取

        def work() -> None:
            try:
                from app import teams_crawl
                r = teams_crawl.crawl_all(scroll_rounds=n, progress=progress)
                teams_crawl.save({
                    "ok": True,
                    "partial": r.get("partial", False),
                    "teams": r.get("teams") or [],
                    "at": r.get("at") or "",
                })
                box.update(r)
            except Exception as e:
                log.warning("Teams 全量抓取失败：%s", e)
                box.update({"ok": False, "reason": str(e)})
            finally:
                self._tm_crawling = False
                self._tm_progress = ""
                self._teams_op = None

        self._tm_progress = "正在整理团队与频道…"
        t = threading.Thread(target=work, daemon=True)
        t.start()
        t.join(timeout=1500)
        return box or {"ok": False, "reason": "抓取超时"}

    def tm_crawl_status(self):
        """全量抓取是否在跑 + 当前进度文字。"""
        return {
            "ok": True,
            "running": bool(getattr(self, "_tm_crawling", False)),
            "message": getattr(self, "_tm_progress", "") or "",
        }

    def tm_channels(self):
        """频道清单（供界面做筛选胶囊）。"""
        try:
            from app import teams_crawl
            st = teams_crawl.stats()
            out = []
            for t in st.get("teams") or []:
                for c in t.get("channels") or []:
                    if c.get("nPosts") or not c.get("error"):
                        out.append({
                            "team": t.get("name") or "",
                            "channel": c.get("name") or "",
                            "nPosts": c.get("nPosts", 0),
                            "nReplies": c.get("nReplies", 0),
                        })
            return {"ok": True, "channels": out, "at": st.get("at") or ""}
        except Exception as e:
            return {"ok": False, "channels": [], "error": str(e)}

    def ec_open_dir(self):
        """打开 EC 附件所在目录。"""
        try:
            from app import teams, config
            config.ensure_dirs()
            teams.EC_DIR.mkdir(parents=True, exist_ok=True)
            os.startfile(str(teams.EC_DIR))       # noqa: S606
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def teams_download_ec(self):
        """下载 EC 频道里的名单 PDF，抽出文字，然后扫一遍关注的名字。

        ★ 这是 EC 功能的**主入口**。整条链路：

            ① 从缓存里认出 EC 频道
               （团队名含 Beijing 101，频道名含 ENGLISH CORNER / EC）
            ② 打开 Teams 窗口（复用 profile —— SharePoint 的登录态在里面）
            ③ 逐个附件下载：
                 分享链接（`:b:`）→ 导航到预览页 → **页面内 fetch** 取字节
               （纯 HTTP 直连拿到的是 26KB 的 HTML，不是 PDF）
            ④ pypdf 抽文字（EC 名单是真文字，不是扫描件）
            ⑤ 存回缓存 + 落一份 .txt 旁文件
            ⑥ 扫一遍用户填的名字 → 命中就亮红点

        ★ 站点文档库那两份（带 `/sites/` 的链接）下不了 ——
          会跳 Microsoft 的二次验证（MFA）。这种情况如实报告，
          不做无谓重试（实测：不完成安全设置的话重试也不通）。
        """
        if getattr(self, "_ec_downloading", False):
            return {"ok": True, "already": True,
                    "note": "上一次下载还在进行中"}
        busy = self._teams_busy("ec")
        if busy:
            return {"ok": False, "busy": True, "reason": busy, "note": busy}
        self._ec_downloading = True
        self._teams_op = "ec"

        box: dict = {}

        def progress(msg: str) -> None:
            self._ec_progress = msg
            log.info("EC：%s", msg)

        def work() -> None:
            try:
                from app import ec_fetch, ec_watch
                r = ec_fetch.download_and_extract(progress=progress)
                box.update(r)
                #  下完就扫 —— 用户要的是「有名字就提醒」，不是「下好了」
                try:
                    ec_watch.scan_recent()
                except Exception as e:
                    log.debug("扫名字失败：%s", e)
                try:
                    box["watch"] = ec_watch.status()
                except Exception:
                    pass
            except Exception as e:
                log.warning("EC 下载失败：%s", e)
                box.update({"ok": False, "note": str(e)})
            finally:
                self._ec_downloading = False
                self._ec_progress = ""
                self._teams_op = None

        self._ec_progress = "准备下载 EC 名单…"
        t = threading.Thread(target=work, daemon=True)
        t.start()
        t.join(timeout=900)
        return box or {"ok": False, "note": "下载超时"}

    def ec_download_status(self):
        """EC 下载是否在跑 + 当前进度文字。"""
        return {
            "ok": True,
            "running": bool(getattr(self, "_ec_downloading", False)),
            "message": getattr(self, "_ec_progress", "") or "",
        }

    def ec_roster(self):
        """当前最新一份 EC 名单的文字（给界面「今日名单」用）。"""
        try:
            from app import ec_fetch
            r = ec_fetch.latest_roster_text()
            if not r.get("ok"):
                return {"ok": False, "note": r.get("note") or "还没有名单",
                        "text": "", "name": ""}
            #  顺手把命中标出来 —— 界面好高亮
            try:
                from app import ec_watch
                names = ec_watch.load_names()
                r["names"] = names
                r["found"] = ec_watch.scan_text(r.get("text") or "", names)
            except Exception:
                r["names"], r["found"] = [], []
            return r
        except Exception as e:
            log.warning("读 EC 名单失败：%s", e)
            return {"ok": False, "note": str(e), "text": "", "name": ""}

    # ---- 晚报（每个工作日晚 9 点自动生成）----
    def digest_status(self):
        """给顶部小红点用：今天是否有未读晚报。"""
        try:
            from app import digest
            return digest.status()
        except Exception as e:
            log.warning("晚报状态读取失败：%s", e)
            return {"ok": False, "unread": False, "has_any": False}

    def digest_latest(self):
        """打开晚报面板：返回最近一份（若已过 21:00 且今天还没生成，就现场生成）。"""
        try:
            from app import digest
            data = pipeline.snapshot()
            made = digest.ensure_today(data)
            cur = made or digest.latest()
            if not cur:
                return {"ok": False, "error": "还没有晚报（工作日晚 9 点后自动生成）"}
            cur = dict(cur)
            cur["ok"] = True
            cur["history"] = [
                {"date": x.get("date"), "weekday": x.get("weekday"),
                 "counts": x.get("counts") or {}}
                for x in digest.list_all(14)
            ]
            return cur
        except Exception as e:
            log.warning("晚报读取失败：%s", e)
            return {"ok": False, "error": str(e)}

    def digest_by_date(self, d: str = ""):
        """按日期取某一期晚报（顶部可翻历史）。"""
        try:
            from app import digest
            for x in digest.list_all(60):
                if x.get("date") == d:
                    out = dict(x)
                    out["ok"] = True
                    return out
            return {"ok": False, "error": "找不到这一期的晚报"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def digest_read(self):
        """标记已读（清掉顶部小红点）。"""
        try:
            from app import digest
            digest.mark_read()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---- 窗口 ----
    def minimize(self):
        try:
            self._window.minimize()
        except Exception:
            pass
        return {"ok": True}

    def win_maximize(self):
        """最大化 / 还原（按钮在自绘标题栏上）。

        ★★★ 这里踩过一个「点了就卡死」的坑，说明白免得改回去 ——

          WinForms 是**单线程**的：控件的属性只能在创建它的
          UI 线程上改。而 js_api 的方法是被 pywebview 从
          **另一个线程**调起来的 —— 在那里直接写：

              n.WindowState = FormWindowState.Maximized

          会**死锁**（不是抛异常，是整个进程卡住）。

          实测复现：单写这一句，进程立刻不动，
          连别处的看门狗定时器都打不出字来。

          → 必须走 `win_effects.ui_thread_call()`，
            它用 WinForms 的 `Control.Invoke` 把操作排队到 UI 线程。
            （pywebview 自己的 maximize() 也是这么做的 ——
              见它 platforms/winforms.py 里那段 InvokeRequired 判断。）
        """
        from app import win_effects

        def work(n):
            from System.Windows.Forms import (  # type: ignore
                FormWindowState, Screen)
            cur = getattr(FormWindowState, "Maximized")
            if n.WindowState == cur:
                n.WindowState = getattr(FormWindowState, "Normal")
                return "normal"
            #  无边框窗最大化默认铺满整屏（连任务栏一起盖）——
            #  先把边界框到工作区。和 win_effects.maximize 同一副药。
            try:
                n.MaximizedBounds = Screen.FromHandle(n.Handle).WorkingArea
            except Exception:
                pass
            n.WindowState = cur
            return "max"

        try:
            box = {"state": None}

            def run(n):
                box["state"] = work(n)

            if win_effects.ui_thread_call(self._window, run) and box["state"]:
                return {"ok": True, "state": box["state"]}
            #  拿不到 native 或切线程失败 → 退回 pywebview 自己的接口
            #  （它内部也是 Invoke，安全）
            self._window.maximize()
            return {"ok": True, "state": "max"}
        except Exception as e:
            log.debug("最大化失败：%s", e)
            return {"ok": False, "error": str(e)}

    def win_close(self):
        """关窗口。

        这里刻意**不退出进程** —— 主循环还在跑自动刷新，
        下次点托盘/快捷方式还能秒开。真要退，用「设置」里的退出。
        """
        try:
            self._window.hide()
            return {"ok": True, "how": "hidden"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def win_quit(self):
        """真退出（设置面板里的「退出程序」）。"""
        try:
            self._window.destroy()
        except Exception:
            pass
        try:
            os._exit(0)          # noqa: SLF001  界面都关了，直接走
        except Exception:
            return {"ok": True}
        return {"ok": True}

    def win_show(self):
        """把窗口显示出来（从隐藏状态回来）。"""
        try:
            self._window.show()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---- 前端就绪心跳（安全网）----

    def win_ready(self):
        """前端报到：「我加载好了，标题栏按钮已就位」。

        ★★ 这个函数是**安全网**，不是装饰。

        原因：窗口开了无边框之后，就**没有系统标题栏**了 ——
        关窗、最小化全靠界面自己画的按钮。
        万一前端因为某个 JS 错误没跑起来，那些按钮就不存在，
        窗口变成一个关不掉的方块，用户只能去任务管理器强杀。

        所以后端建窗后会开一个定时器：
          等 ``READY_TIMEOUT`` 秒，如果这个函数没被调用过，
          就把窗口退回「有系统边框」的样子 ——
          至少还给用户一个能点的关闭键。
        """
        self._front_ready = True
        log.info("前端已就绪")
        return {"ok": True}

    # ---- 窗口形态 ----

    def win_cfg(self):
        """当前的窗口偏好（形态 / 毛玻璃 / 置顶 / 贴右上角）。"""
        try:
            cfg = _load_window_cfg()
            return {"ok": True, "cfg": cfg,
                    "shape": getattr(self, "_shape", cfg.get("shape")),
                    "glass_ok": _glass_status()}
        except Exception as e:
            return {"ok": False, "error": str(e), "cfg": {}}

    def win_set(self, key: str, value=None):
        """改一项窗口偏好并立刻生效。

        能改的：
            shape      full / compact —— 切形态（会调整窗口大小与位置）
            glass      true / false   —— 毛玻璃开关
            on_top     true / false   —— 置顶开关
            pin_right  true / false   —— 摆到右上角
        """
        key = str(key or "").strip()
        if key not in ("shape", "glass", "on_top", "pin_right"):
            return {"ok": False, "error": f"改不了这一项：{key}"}

        cfg = _load_window_cfg()
        cfg[key] = value
        _save_window_cfg(cfg)
        self._window_cfg = cfg
        log.info("窗口偏好改成 %s = %r", key, value)

        from app import win_effects

        #  ── 切形态 ──
        if key == "shape":
            shape = str(value or "full")
            if shape not in _WINDOW_PRESETS:
                return {"ok": False, "error": f"没有这个形态：{shape}"}
            self._shape = shape
            sizes = _window_sizes(shape)

            #  ★ 一、先改尺寸下限。
            #    建窗时 pywebview 把 min_size 交给了 WinForms 的
            #    MinimumSize（物理像素），它自己会拦住改尺寸的请求。
            #    不先放开下限，下面的 resize 根本推不动 ——
            #    实测：min_size=(370,500) 在 2.0 缩放下变成
            #    MinimumSize = 740x1000，小浮窗想缩到 340 就被卡住。
            pre = _WINDOW_PRESETS.get(shape) or {}
            rng = win_effects.set_size_range(self._window,
                                             min_css=pre.get("min"),
                                             max_css=pre.get("max"))

            #  ★ 二、改窗口大小（CSS 尺寸，后端换算）。
            ok_r = win_effects.resize_css(self._window,
                                          sizes["w"], sizes["h"])
            time.sleep(0.12)          # 让系统把新尺寸落实
            #  ★ 三、再摆位置。
            #    顺序不能反 —— 先摆位置的话，那时窗口还是旧尺寸，
            #    算出来的坐标是基于旧宽高的，新尺寸一生效就偏了。
            corner = "top-right" if shape == "compact" else "center"
            r = win_effects.pin_to_corner(self._window, corner)

            #  ★ ok 只看 resize —— 那才是「切形态」的关键。
            #    pin_to_corner 的 ok 会误报（SetWindowPos 在
            #    窗口动画途中返回 0，但位置其实已经摆好了），
            #    把它 AND 进去会让前端误弹「没能切换窗口大小」。
            return {"ok": bool(ok_r), "shape": shape,
                    "size": [sizes["w"], sizes["h"]],
                    "range": rng,
                    "pin_ok": bool(r.get("ok")),
                    "pos": [r.get("x"), r.get("y")],
                    "real": [r.get("w"), r.get("h")],
                    "scale": r.get("scale")}

        #  ── 置顶 ──
        if key == "on_top":
            ok = win_effects.set_always_on_top(self._window, bool(value))
            #  pywebview 自己的属性也设一下，两处一起才稳。
            #  ★ 走 ui_thread_call —— 直接写 native.TopMost 同样会死锁
            win_effects.ui_thread_call(
                self._window,
                lambda n: setattr(n, "TopMost", bool(value)))
            return {"ok": ok, "on_top": bool(value)}

        #  ── 贴右上角 ──
        if key == "pin_right":
            if not value:
                return {"ok": True, "pin_right": False}
            try:
                r = win_effects.pin_to_corner(self._window, "top-right")
                if r.get("ok"):
                    shape = getattr(self, "_shape", "compact")
                    cfg[f"{shape}_pos"] = [r["x"], r["y"]]
                    _save_window_cfg(cfg)
                return {"ok": bool(r.get("ok")),
                        "pos": [r.get("x"), r.get("y")],
                        "real": [r.get("w"), r.get("h")],
                        "scale": r.get("scale"),
                        "screen": r.get("screen"),
                        "why": r.get("why")}
            except Exception as e:
                log.debug("贴右上角失败：%s", e)
                return {"ok": False, "error": str(e)}

        #  ── 毛玻璃 ──
        if key == "glass":
            if value:
                r = win_effects.apply_glass(self._window, "acrylic")
                _set_glass_status(bool(r.get("ok")))
                return {"ok": bool(r.get("ok")),
                        "glass": r.get("how") or r.get("why")}
            win_effects.apply_glass(self._window, "none")
            _set_glass_status(False)
            return {"ok": True, "glass": "closed"}

        return {"ok": True}

    def win_resize(self, width, height):
        """按当前形态记住用户调的大小（下次启动照旧）。"""
        try:
            w = max(200, min(int(width), 4000))
            h = max(160, min(int(height), 4000))
        except Exception:
            return {"ok": False, "error": "尺寸不是数字"}
        cfg = _load_window_cfg()
        shape = getattr(self, "_shape", cfg.get("shape") or "full")
        #  ★ 0.5.1：按形态的量程夹取后再存。
        #    前端的 resize 监听在「形态切换途中」也会触发 ——
        #    那时类已经是 compact、窗口还是大尺寸，不夹就会把
        #    大尺寸存成小浮窗的「用户偏好」，下次开机界面和
        #    窗口对不上（留白 + 按钮出屏）。
        pre = _WINDOW_PRESETS.get(shape) or {}
        mn, mx = pre.get("min"), pre.get("max")
        if mn:
            w, h = max(w, mn[0]), max(h, mn[1])
        if mx:
            w, h = min(w, mx[0]), min(h, mx[1])
        else:
            #  full 没写上限，但不能存一个比工作区还大的尺寸
            try:
                from app import win_effects as _we
                wa = _we.work_area()
                s0 = _we.dpi_scale(self._window) or 1.0
                w = min(w, int(wa["w"] / s0))
                h = min(h, int(wa["h"] / s0))
            except Exception:
                pass
        cfg[f"{shape}_size"] = [w, h]
        _save_window_cfg(cfg)
        return {"ok": True, "size": [w, h]}

    def win_drag(self, dx, dy):
        """自绘拖动的位移接口（0.5.1）。

        ★ 为什么存在：pywebview 内建拖动（.pywebview-drag-region）
          在这条链路（内建 http 分发 + 无边框 + WinForms）上实测不生效，
          window.move 调了窗口也不动。所以前端自己算增量送过来，
          这里走 win_effects.move_by（SetWindowPos）。

        增量是 **CSS 像素**（前端 screenX 的差值），后端乘 DPI。
        """
        try:
            dx = max(-400, min(int(dx), 400))   # 夹一下，防前端算疯
            dy = max(-400, min(int(dy), 400))
        except Exception:
            return {"ok": False, "error": "位移不是数字"}
        try:
            from app import win_effects
            ok = win_effects.move_by(self._window, dx, dy)
            return {"ok": bool(ok)}
        except Exception as e:
            log.debug("win_drag 失败：%s", e)
            return {"ok": False, "error": str(e)}

    # ══════════ 使用教程「看过了」标记 ══════════
    def tour_status(self):
        """教程看过了吗。

        ★ 前端在启动时会问一次：
            · 后端说看过 → 不弹
            · 后端说没看过 → 再看一眼 localStorage，两边都不记得才弹
          所以两边只要有一边记得，就不会再骚扰用户。
        """
        try:
            return {"ok": True,
                    "done": bool(_load_window_cfg().get("tour_done"))}
        except Exception as e:
            log.debug("读教程标记失败：%s", e)
            return {"ok": False, "done": False}

    def tour_done(self):
        """记下「教程看过了」（用户点了跳过 / 走完 / 按 Esc 退）。"""
        try:
            cfg = _load_window_cfg()
            cfg["tour_done"] = True
            _save_window_cfg(cfg)
            return {"ok": True}
        except Exception as e:
            log.debug("存教程标记失败：%s", e)
            return {"ok": False, "error": str(e)}

    def tour_reset(self):
        """把标记清掉（设置里「看一遍使用教程」用不到，
        留着是为了排查问题时能手动重现「首次运行」。）"""
        try:
            cfg = _load_window_cfg()
            cfg["tour_done"] = False
            _save_window_cfg(cfg)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _win_size(self):
        """读当前窗口宽高。

        ★ 三个来源依次试，不能只信一个 ——
          实测 ``native.Width`` 在某些时刻返回 None
          （窗口刚建好、或正在做最大化动画），
          直接 ``int()`` 会抛 ``TypeError: NoneType``。
        """
        #  ① 直接读客户区（最准，而且不受窗口装饰影响）
        try:
            from app import win_effects
            w, h = win_effects.client_size(self._window)
            if w > 0 and h > 0:
                return w, h
        except Exception:
            pass

        #  ② pywebview 自己的属性
        try:
            w = getattr(self._window, "width", None)
            h = getattr(self._window, "height", None)
            if isinstance(w, int) and isinstance(h, int) and w > 0 and h > 0:
                return w, h
        except Exception:
            pass

        #  ③ 后端窗体（.NET Form）
        try:
            n = getattr(self._window, "native", None)
            if n is not None:
                w = getattr(n, "Width", None)
                h = getattr(n, "Height", None)
                if w is not None and h is not None:
                    w, h = int(w), int(h)
                    if w > 0 and h > 0:
                        return w, h
        except Exception as e:
            log.debug("读 native 尺寸失败：%s", e)

        #  ④ 按当前形态的默认值兜底
        try:
            shape = getattr(self, "_shape", "full")
            s = _window_sizes(shape)
            return int(s["w"]), int(s["h"])
        except Exception:
            return 0, 0

    # ---- 跳转：用**组件自己的** Edge（已经登录过）----
    def open_url(self, url: str):
        """打开链接 ——  先过安全校验，阻止「退出/删除/上传/提交」类操作。

        参考 CampusDesk 的做法：只允许「查看页面」类链接通过。
        这不是防坏人，是**防解析出错** —— 页面结构一变，
        可能顺手抓到旁边的「删除」按钮链接，用户点下去就出事了。

        ★ 用组件自己的 Edge profile 打开（不是系统默认浏览器）：
          抓数据用的就是那个 profile，登录态在里面 ——
          所以点进去直接就是登录状态，不用再输一遍账号密码。

          自己的 Edge 仍然照常能用：两个实例用不同的 user-data-dir，
          Edge 原生支持并存。
        """
        if not url:
            return {"ok": False}

        try:
            from app import safelink

            safe = safelink.sanitize(url)
        except Exception as e:
            log.warning("链接校验异常：%s", e)
            safe = url                      # 校验器本身出错时不拦（保功能）

        if not safe:
            log.warning("已阻止打开不安全链接：%s", url[:160])
            return {"ok": False, "blocked": True,
                    "error": "这个链接可能是「退出/删除」类操作，为安全起见没有打开"}

        #  ① 首选：组件自己的 Edge（带登录态）
        try:
            from app import opener
            r = opener.open_in_own_profile(safe, reuse=True)
            if r.get("ok"):
                log.info("打开（%s）：%s", r.get("how"), safe[:140])
                return {"ok": True, "how": r.get("how")}
            log.warning("自带 profile 打开失败（%s），退回系统浏览器",
                        r.get("error") or r.get("how"))
        except Exception as e:
            log.warning("自带 profile 打开异常，退回系统浏览器：%s", e)

        #  ② 兜底：系统默认浏览器（至少能打开，虽然要重新登录）
        try:
            webbrowser.open(safe, new=2)
            log.info("打开（系统浏览器）：%s", safe[:140])
            return {"ok": True, "how": "system"}
        except Exception as e:
            log.warning("打开失败：%s", e)
            return {"ok": False, "error": str(e)}

    # ---- 数据与备份（导出 / 导入）----
    def backup_status(self):
        """给「数据与备份」面板用：当前有多少可备份的数据。"""
        try:
            from app import backup
            return backup.status()
        except Exception as e:
            log.warning("读取备份状态失败：%s", e)
            return {"ok": False, "error": str(e)}

    def backup_export(self):
        """导出到桌面。返回 {ok, path, counts, bytes}。"""
        try:
            from app import backup
            return backup.export()
        except Exception as e:
            log.warning("导出失败：%s", e)
            return {"ok": False, "error": str(e)}

    def backup_import(self, path: str = ""):
        """导入指定备份文件（默认合并，不覆盖现有记录）。"""
        if not path:
            return {"ok": False, "error": "没有指定文件"}
        try:
            from app import backup
            return backup.import_from(path, merge=True)
        except Exception as e:
            log.warning("导入失败：%s", e)
            return {"ok": False, "error": str(e)}

    def pick_backup_file(self):
        """ 用 Windows 原生「打开文件」对话框让用户挑备份文件。

        为什么不用 HTML 的 <input type=file>：
          pywebview 里拿不到真实路径（浏览器安全限制），
          而导入需要路径。所以走 tkinter 的原生对话框
          —— 这也是 Windows 用户的习惯交互。
        """
        try:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)      # 保证对话框在最前面
            path = filedialog.askopenfilename(title="选择备份文件",
                filetypes=[("备份文件", "*.json"), ("所有文件", "*.*")],
                initialdir=str(self._desktop_dir()),
            )
            root.destroy()
            if not path:
                return ""                          # 用户取消
            return str(path)
        except Exception as e:
            log.warning("打开文件选择器失败：%s", e)
            return ""

    @staticmethod
    def _desktop_dir():
        from pathlib import Path
        import os
        for cand in (Path(os.environ.get("USERPROFILE", "")) / "Desktop",
            Path(os.environ.get("USERPROFILE", "")) / "OneDrive" / "Desktop",
        ):
            if cand.exists():
                return cand
        return Path(os.environ.get("USERPROFILE", "."))

    # ---- 登录：修复（用户要求「不要让我自己跑脚本」）----
    def relogin(self):
        """点一下就把两个登录态都恢复。

        后台线程跑，不阻塞界面。完成后前端会通过 poll 自动看到新数据。
        """
        if getattr(self, "_relogging", False):
            return {"ok": True, "already": True}

        self._relogging = True

        def work() -> None:
            try:
                from app import autologin, pipeline

                log.info("用户点击「重新登录」，开始自动恢复登录态")
                res = autologin.ensure_all(reason="user_click")

                # 恢复成功 → 立即抓一次，让界面刷新
                if (res.get("mb") or {}).get("ok"):
                    try:
                        pipeline.refresh_sync()
                    except Exception as e:
                        log.warning("重新登录后抓取失败：%s", e)
            except Exception as e:
                log.warning("重新登录失败：%s", e)
            finally:
                self._relogging = False

        threading.Thread(target=work, daemon=True).start()
        return {"ok": True, "started": True}

    def login_status(self):
        """给界面用：两个登录态健康度，以及是否有失败在退避中。"""
        try:
            from app import autologin
            return autologin.status()
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def show_login_help(self):
        """兼容旧调用 —— 直接触发自动登录，而不是让用户敲命令。"""
        return self.relogin()

    # ---- 「我已完成，不再提示」 ----
    def mark_done(self, url: str = "", title: str = "", course: str = ""):
        """手动标记为已完成（线下交纸质版、老师未给分的情况）。

        只存本地，**不会**写入 ManageBac。
        """
        try:
            from app import manual_done

            ok = manual_done.mark(url, title, course)
            pipeline.apply_manual_marks()
            pipeline.save_cache_now()
            log.info("手动完成：%s", title or url)
            return {"ok": ok, "total": manual_done.count()}
        except Exception as e:
            log.warning("标记完成失败：%s", e)
            return {"ok": False, "error": str(e)}

    def unmark_done(self, url: str = "", title: str = "", course: str = ""):
        """撤销手动标记，回到「已提交 / 已给分」的原始判定。"""
        try:
            from app import manual_done

            ok = manual_done.unmark(url, title, course)
            pipeline.apply_manual_marks()
            pipeline.save_cache_now()
            log.info("撤销手动完成：%s", title or url)
            return {"ok": ok, "total": manual_done.count()}
        except Exception as e:
            log.warning("撤销标记失败：%s", e)
            return {"ok": False, "error": str(e)}

    def manual_count(self):
        try:
            from app import manual_done

            return {"ok": True, "count": manual_done.count()}
        except Exception as e:
            return {"ok": False, "count": 0, "error": str(e)}

    # ---- 任务详情（点击卡片展开）----
    def task_detail(self, url: str = "", force: bool = False):
        """抓取任务的附件 / 老师要求 / 成绩。

        结果按任务缓存 24 小时 —— 点第二次是几乎立即的。
        """
        try:
            from app import taskdetail

            return taskdetail.fetch(url, force=bool(force))
        except Exception as e:
            log.warning("取任务详情失败：%s", e)
            return {"ok": False, "error": str(e), "attachments": [],
                    "description": "", "grade": {}}

    # ---- 资料（Files 实时浏览 / 下载）----
    def files_list(self, cid: str, fid: str = "", force: bool = False):
        """列出课程 Files 的目录内容（含子文件夹）。

         实时向 ManageBac 请求；force=True 时绕过缓存。
        """
        try:
            from app import files

            return files.list_files(cid, fid or "", use_cache=not force)
        except Exception as e:
            log.warning("列 Files 失败：%s", e)
            return {"ok": False, "error": str(e), "folders": [],
                    "files": [], "items": [], "total": 0}

    def files_download(self, item: dict):
        """下载单个文件（后台线程，立刻返回）。"""
        if not isinstance(item, dict) or not item.get("url"):
            return {"ok": False, "error": "参数不正确"}
        threading.Thread(target=self._dl_file_item, args=(item,),
                         daemon=True).start()
        return {"ok": True, "started": True}

    def _dl_file_item(self, item: dict) -> None:
        try:
            from app import files

            # 保存到桌面/ManageBac 资料/<课程名>/<文件夹路径>/
            cls_name = (item.get("className") or "").strip()
            sub = (item.get("subDir") or "").strip()
            root = files.save_root(cls_name)
            if sub:
                for part in sub.split("/"):
                    part = part.strip()
                    if part and part not in (".", ".."):
                        root = root / files.safe_name(part)

            res = files.download_item(item, dest_dir=root,
                                      referer=item.get("referer", ""))
            if res.get("ok"):
                size = res.get("size") or 0
                self._notify({"ok": True, "count": 1, "total": 1,
                              "folder": str(root)}, res.get("name", ""),
                             extra=f"{size/1024:.0f} KB")
            else:
                self._notify({"ok": False, "count": 0, "total": 1,
                              "folder": "", "error": res.get("error")},
                             res.get("name", ""))
        except Exception as e:
            log.exception("下载文件项失败")
            self._notify({"ok": False, "count": 0, "total": 1,
                          "folder": "", "error": str(e)}, item.get("name", ""))

    def files_download_all(self, cid: str, fid: str = "",
                           cls_name: str = "", folder: str = ""):
        """下载当前目录里的**全部文件**（不含子文件夹，后台执行）。"""
        try:
            from app import files

            d = files.list_files(cid, fid or "", use_cache=False)
            if not d.get("ok"):
                return {"ok": False, "error": d.get("error") or "读取失败"}
            items = d.get("files") or []
            if not items:
                return {"ok": False, "error": "本目录没有文件"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

        threading.Thread(target=self._dl_items_all,
            args=(items, cls_name, folder), daemon=True
        ).start()
        return {"ok": True, "started": True, "total": len(items)}

    def _dl_items_all(self, items: list, cls_name: str,
                      folder: str) -> None:
        try:
            from app import files

            root = files.save_root(cls_name)
            if folder:
                for part in folder.split("/"):
                    part = part.strip()
                    if part and part not in (".", ".."):
                        root = root / files.safe_name(part)

            ok = 0
            for it in items:
                try:
                    r = files.download_item(it, dest_dir=root)
                    if r.get("ok"):
                        ok += 1
                except Exception as e:
                    log.warning("下载 %s 失败：%s", it.get("name"), e)

            self._notify({"ok": ok > 0, "count": ok, "total": len(items),
                          "folder": str(root)},
                         folder or cls_name or "资料")
        except Exception as e:
            log.exception("批量下载失败")
            self._notify({"ok": False, "count": 0, "total": len(items),
                          "folder": "", "error": str(e)}, "资料")

    # ---- 下载 ----
    def download_task(self, cid: str, tid: str, title: str = ""):
        """下载某任务的全部附件（后台线程，避免卡住界面）。"""
        threading.Thread(target=self._dl_task, args=(cid, tid, title), daemon=True
        ).start()
        return {"ok": True, "started": True}

    def _dl_task(self, cid: str, tid: str, title: str) -> None:
        try:
            from app import download as dl

            res = dl.download_task(cid, tid, title)
            self._notify(res, title)
        except Exception as e:
            log.exception("下载失败")
            self._alert(f"下载失败：{e}")

    def download_files(self, cid: str, name: str = ""):
        """下载某课程 Files 里的全部文件。"""
        threading.Thread(target=self._dl_files, args=(cid, name), daemon=True
        ).start()
        return {"ok": True, "started": True}

    def _dl_files(self, cid: str, name: str) -> None:
        try:
            from app import download as dl

            res = dl.download_files(cid, name)
            self._notify(res, name)
        except Exception as e:
            log.exception("下载失败")
            self._alert(f"下载失败：{e}")

    def list_files(self, cid: str, folder: str = ""):
        """列出课程 Files 的内容（**纯 HTTP，不启动浏览器**）。

         历史坑：这个方法的旧实现走 `app/download.py` 的 `list_files()`，
          那个用 CDP 驱动浏览器 → 前端一进「资料」页就会拉起一个
          无头 Edge，用户看着莫名（实测确实发生过）。
          现在统一走 `app/files.py`（纯 HTTP + 缓存）。
        """
        try:
            from app import files

            r = files.list_files(cid, folder or "", use_cache=True)
            if not r.get("ok"):
                return {"ok": False, "items": [],
                        "error": r.get("error") or "读取失败"}
            return {"ok": True, "items": r.get("items") or []}
        except Exception as e:
            log.warning("列出 Files 失败：%s", e)
            return {"ok": False, "error": str(e), "items": []}

    def download_file(self, url: str, filename: str = "", subdir: str = ""):
        """下载单个文件（Files 里的某一项）。"""
        threading.Thread(target=self._dl_one, args=(url, filename, subdir), daemon=True
        ).start()
        return {"ok": True, "started": True}

    def _dl_one(self, url: str, filename: str, subdir: str) -> None:
        try:
            from app import download as dl

            root = dl.download_root()
            if subdir:
                root = root / dl.safe_name(subdir)
            root.mkdir(parents=True, exist_ok=True)
            name = dl.safe_name(filename or dl.guess_name(url))
            dest = root / name
            cookies = dl.browser_cookies()
            res = dl.download_one(url, dest, cookies)
            if res.get("ok"):
                self._notify({"ok": True, "count": 1, "total": 1,
                              "folder": str(root)}, name)
            else:
                self._alert(f"下载失败：{res.get('error')}")
        except Exception as e:
            log.exception("下载失败")
            self._alert(f"下载失败：{e}")

    def open_folder(self, path: str):
        try:
            from app import download as dl

            dl.open_folder(path)
        except Exception:
            pass
        return {"ok": True}

    # ---- 内部：结果提示 ----
    @staticmethod
    def _notify(res: dict, label: str, extra: str = "") -> None:
        try:
            import webview as _wv

            if res.get("ok"):
                folder = (res.get("folder") or "").replace("\\", "\\\\")
                _wv.windows[0].evaluate_js(f"window.downloadDone && window.downloadDone("
                    f"{json.dumps(res.get('count', 0))}, "
                    f"{json.dumps(res.get('total', 0))}, "
                    f"{json.dumps(label)}, {json.dumps(folder)}, "
                    f"{json.dumps(extra)})"
                )
            else:
                _wv.windows[0].evaluate_js(f"window.downloadDone && window.downloadDone("
                    f"0, 1, {json.dumps(label)}, '', '', "
                    f"{json.dumps(res.get('error') or '未知原因')})"
                )
        except Exception:
            pass

    @staticmethod
    def _alert(msg: str) -> None:
        try:
            import webview as _wv

            _wv.windows[0].evaluate_js(f"alert({json.dumps(msg)})")
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════
#  启动前环境检查
#
#  ★ 为什么需要
#    程序依赖 WebView2 Runtime。Win11 与较新 Win10 自带，老 Win10 可能没有。
#    没装时 pywebview 会抛异常，而 exe 是 console=False（不弹黑框），
#    用户看到的是「双击了，什么都没发生」—— 不知如何是好。
#
#    所以要在启动前检查，并用**原生对话框**给出能照着做的指引。
#    （不能用网页提示 —— 那也需要 WebView2 才能显示。）
# ══════════════════════════════════════════════════════════════════

WEBVIEW2_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"


def _webview2_installed() -> bool:
    """检测 WebView2 Runtime 是否已安装。

    两种方式都试：
      ① 查注册表（官方推荐，最可靠）
      ② 查安装目录（注册表被清理过时的兜底）
    """
    # ① 注册表
    try:
        import winreg
        keys = [
            r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
            r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
            # 每用户安装（非管理员权限装的那种）
            r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
        ]
        for k in keys:
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    with winreg.OpenKey(hive, k) as h:
                        v, _ = winreg.QueryValueEx(h, "pv")
                        if v and v != "0.0.0.0":
                            return True
                except OSError:
                    continue
    except Exception:
        pass

    # ② 安装目录
    import os
    for base in (
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("LOCALAPPDATA", ""),
    ):
        if not base:
            continue
        p = Path(base) / "Microsoft" / "EdgeWebView" / "Application"
        try:
            if p.is_dir() and any(
                d.name[0].isdigit() for d in p.iterdir() if d.is_dir()
            ):
                return True
        except Exception:
            continue

    return False


def _show_missing_webview2() -> None:
    """用原生对话框告知用户缺什么、怎么装。

    ★ 这里刻意用 ctypes 调 Windows 的 MessageBox，
      而不是 tkinter —— tkinter 在打包版里可能没打进去，
      而 MessageBox 是系统自带的，一定会弹出来。
    """
    msg = (
        "缺少一个运行组件：Microsoft WebView2 Runtime\n\n"
        "这个组件是 Windows 自带的浏览器内核，"
        "大部分电脑已经有了（Win11 全都自带）。\n\n"
        "安装方法：\n"
        "  1. 点「确定」后会自动打开下载页面\n"
        "  2. 下载并运行安装程序（一路点「下一步」即可）\n"
        "  3. 装完重新双击本程序\n\n"
        "下载地址（如果没自动打开，可手动复制到浏览器）：\n"
        + WEBVIEW2_URL
    )
    try:
        import ctypes
        # MB_OK | MB_ICONINFORMATION
        ctypes.windll.user32.MessageBoxW(
            None, msg, "ManageBac-packer - 缺少运行组件", 0x40
        )
    except Exception:
        # 连对话框都弹不出来时，至少写到日志
        try:
            log.error("缺少 WebView2 Runtime，且无法弹出提示对话框")
        except Exception:
            pass

    # 打开下载页
    try:
        import webbrowser
        webbrowser.open(WEBVIEW2_URL)
    except Exception:
        pass


def _run_warmup_only() -> int:
    """静默预热：不开窗口，后台登录 + 抓一次，写完缓存就退出。

    由开机启动项调用（见 autostart_install）。全程无界面，
    所以任何异常都必须吞掉 —— 开机时报错弹框会吓到用户。
    日志照样写（logging 是全局的），真出问题在日志里能查到。
    """
    try:
        from app import warmup as _w
        res = _w.startup_prepare(verbose=False)
        log.info("静默预热完成：%s", (res or {}).get("message") or "ok")
    except Exception as e:
        log.warning("静默预热失败（忽略）：%s", e)
    return 0


# ══════════ 开机自启（打包版自己管理，不依赖 make_startup.py）══════════
#
#  为什么不在打包时用 make_startup.py：
#    那个脚本是给源码环境写的 —— 它找 .venv 的 pythonw.exe、
#    调 warmup.py。exe 版没有这些，宿主就是 exe 自己。
#
#  做法：在「启动」文件夹放一个 .lnk，指向 exe 自己 + --warmup。
#    为什么不用注册表 Run 键：启动文件夹用户看得见、能自己删，
#    出问题时好排查；注册表藏在深处，反而不好收拾。

AUTOSTART_LNK_NAME = "ManageBac-packer 预热.lnk"


def _startup_dir() -> Path:
    return Path(os.environ.get("APPDATA", "")) / (
        r"Microsoft\Windows\Start Menu\Programs\Startup")


def _host_command() -> tuple[str, str]:
    """返回 (要启动的程序, 附加参数)。

    打包版 → (exe 自身, "--warmup")
    源码版 → (pythonw.exe, "warmup.py --quiet")
    """
    if getattr(sys, "frozen", False):
        return sys.executable, "--warmup"
    py = Path(sys.executable)
    pw = py.with_name("pythonw.exe")
    if pw.exists():
        py = pw
    return str(py), f'"{Path(__file__).resolve().parent / "warmup.py"}" --quiet'


def _make_lnk(lnk: Path, target: str, args: str, workdir: str) -> bool:
    """用 PowerShell 的 WScript.Shell 建快捷方式。"""
    import subprocess as _sp
    ps = (
        "$W = New-Object -ComObject WScript.Shell; "
        f"$s = $W.CreateShortcut('{lnk}'); "
        f"$s.TargetPath = '{target}'; "
        f"$s.Arguments = '{args}'; "
        f"$s.WorkingDirectory = '{workdir}'; "
        f"$s.WindowStyle = 7; "
        "$s.Description = 'ManageBac-packer 开机后台预热'; "
        "$s.Save(); Write-Output 'ok'"
    )
    try:
        r = _sp.run(["powershell", "-NoProfile", "-NonInteractive",
                     "-Command", ps],
                    capture_output=True, text=True, timeout=30)
        return lnk.exists()
    except Exception as e:
        log.warning("创建开机启动快捷方式失败：%s", e)
        return False


def autostart_status() -> dict:
    """开机自启当前是什么状态。"""
    lnk = _startup_dir() / AUTOSTART_LNK_NAME
    return {
        "installed": lnk.exists(),
        "path": str(lnk),
        "supported": sys.platform == "win32",
    }


def autostart_install() -> dict:
    """装开机自启。失败时返回可读原因，不抛异常。"""
    if sys.platform != "win32":
        return {"ok": False, "error": "只有 Windows 支持开机自启"}
    try:
        d = _startup_dir()
        d.mkdir(parents=True, exist_ok=True)
        target, args = _host_command()
        lnk = d / AUTOSTART_LNK_NAME
        ok = _make_lnk(lnk, target, args, str(Path(target).parent))
        if ok:
            log.info("已安装开机自启：%s", lnk)
            return {"ok": True, "path": str(lnk)}
        return {"ok": False, "error": "快捷方式没能创建成功"}
    except Exception as e:
        log.warning("安装开机自启失败：%s", e)
        return {"ok": False, "error": str(e)}


def autostart_uninstall() -> dict:
    """卸掉开机自启。"""
    try:
        lnk = _startup_dir() / AUTOSTART_LNK_NAME
        if lnk.exists():
            lnk.unlink()
            log.info("已卸载开机自启")
            return {"ok": True}
        return {"ok": True, "already": True}
    except Exception as e:
        log.warning("卸载开机自启失败：%s", e)
        return {"ok": False, "error": str(e)}


# ══════════════════════════════════════════════════════════════
#  窗口形态与外观偏好（v0.5.0）
#
#  两种形态：
#    完整   —— 现在的样子，七个标签页，能看全部内容
#    小浮窗 —— 一张小卡片，只有关键几条，默认贴右上角浮在别的应用上
#
#  偏好存 data/window.json，下次启动照旧。
#  存文件而不是存 localStorage 的原因：窗口尺寸得在**建窗之前**就知道，
#  那时前端还没跑起来。
# ══════════════════════════════════════════════════════════════

WINDOW_CFG_FILE = config.DATA_DIR / "window.json"

#  ★★ 无边框的前端就绪标志。
#
#  为什么要这个开关：
#    无边框意味着「窗口没有系统标题栏」——
#    没有标题栏就没有最小化/最大化/关闭按钮，也拖不动。
#    这些必须由界面自己画出来。
#    后端先开 frameless、前端还没画，用户看到的就是
#    「白屏、点不动、像卡死」（这是真踩到的）。
#
#    所以顺序必须是：前端标题栏做好、验证过 → 再把这里改成 True。
#    没改之前，即使配置里写了 frameless，也会被忽略。
#
#  v0.5.0：前端标题栏已做好（右上角有最小化/最大化/关闭，
#          标题栏可拖），并加了「后端没人报到就装回边框」的安全网，
#          所以这里可以开了。
FRONTEND_HAS_TITLEBAR = True

#  前端就绪的等待时长（秒）。
#  超时就把系统边框装回去 —— 免得到前端挂了、窗口关不掉。
READY_TIMEOUT = 12.0

#  各形态的默认尺寸。
#  min 是下限，拖太小会把内容挤坏。
_WINDOW_PRESETS = {
    "full": {
        "w": W, "h": H,
        "min": (370, 500),
    },
    "compact": {
        #  ★ 0.5.1 改成横版小卡片（520x320 ≈ 16:10）：
        #    用户反馈竖版 340x420 里内容只填一半、底部按钮
        #    会被顶到任务栏以下看不见。横版配网格布局刚好铺满。
        #  ★ 收尾又调了一轮（480x300 → 520x320，下限 360x250 → 400x290）：
        #    旧下限 250 高装不下卡片内容（课名 + 标签 + 时间行），
        #    overflow 会把「晚自习」三个字啃掉一半 ——
        #    下限的意义是「默认内容完整显示」，不是「能开就行」。
        "w": 520, "h": 320,
        "min": (400, 290),
        "max": (820, 620),
    },
}


def _load_window_cfg() -> dict:
    """读窗口偏好；文件不在或坏了就用默认值。"""
    default = {
        "shape": "full",         # full / compact
        #  毛玻璃默认**关**。
        #  它需要 transparent 窗口，而透明窗口在没有
        #  DWM 材质支持的机器上会变成一块黑 —— 那是不可逆的破坏。
        #  想用就在设置里自己开，开了会记下来。
        "glass": False,
        "on_top": False,         # 置顶
        "pin_right": False,      # 开机摆到右上角
        #  无边框默认**开**（前端已有自绘标题栏 + 安全网）。
        "frameless": True,
        #  使用教程「看过了」的标记。
        #  ★ 为什么存在这里而不是只存 localStorage：
        #    localStorage 现在能留住了（见 webview.start 那段），
        #    但它是跟着 WebView2 的缓存目录走的 ——
        #    用户清一下缓存、或者换了缓存目录，就全没了。
        #    教程这种「弹一次就别再弹」的东西，
        #    留个服务端副本更稳（两边只要有一边记得就算看过）。
        "tour_done": False,
        #  用户自己调过的尺寸 / 摆过的位置（win_resize / pin_right 写入）。
        #  ★ 这几个键必须列在 default 里：下面的白名单只保留
        #    default 已有的键，不列的话存了也读不回来 ——
        #    用户拖好的小浮窗尺寸每次开机都被打回默认。
        #    None 表示「还没存过」，_window_sizes 校验不过就回预设。
        "full_size": None, "full_pos": None,
        "compact_size": None, "compact_pos": None,
    }
    if not WINDOW_CFG_FILE.exists():
        return default
    try:
        d = json.loads(WINDOW_CFG_FILE.read_text(encoding="utf-8"))
        if not isinstance(d, dict):
            return default
        out = dict(default)
        out.update({k: v for k, v in d.items() if k in default})
        if out.get("shape") not in _WINDOW_PRESETS:
            out["shape"] = "full"
        #  前端标题栏没做好之前，一律不给无边框 ——
        #  否则窗口会变成一个动不了的方块。
        if not FRONTEND_HAS_TITLEBAR:
            out["frameless"] = False
            out["glass"] = False
        return out
    except Exception as e:
        log.debug("读窗口偏好失败（用默认值）：%s", e)
        return default


def _save_window_cfg(d: dict) -> None:
    try:
        config.ensure_dirs()
        WINDOW_CFG_FILE.write_text(
            json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        log.warning("存窗口偏好失败：%s", e)


def _window_sizes(shape: str) -> dict:
    """算出一个形态的窗口尺寸与位置。

    ★ 尺寸用 **CSS 像素**（界面上说的宽高）——
      建窗时 pywebview 收的就是这个单位。

    ★ 位置分两种情况，不能混：
        ① pywebview **还没启动**（建窗之前）：
           系统报的是逻辑坐标，和 create_window(x=, y=) 一致 → 直接给
        ② pywebview **已经启动**（运行时切形态）：
           系统报的是物理坐标，要另外算 ——
           那条路走 ``win_effects.pin_to_corner()``，不读这里的位置。
      所以这里只负责第 ① 种。
    """
    from app import win_effects

    pre = _WINDOW_PRESETS.get(shape) or _WINDOW_PRESETS["full"]
    out = dict(pre)

    cfg = _load_window_cfg()
    #  用户手动调过大小 → 用他调的
    key = f"{shape}_size"
    saved = cfg.get(key)
    if (isinstance(saved, (list, tuple)) and len(saved) == 2
            and all(isinstance(v, int) and v > 0 for v in saved)):
        out["w"], out["h"] = int(saved[0]), int(saved[1])
        #  ★ 0.5.1：存的尺寸要按这个形态的量程夹一遍。
        #    踩过的坑：前端在「形态已经是 compact、窗口还没缩小」的
        #    间隙把 745 高的尺寸写进了 compact_size，之后每次开机
        #    都是「小浮窗界面 + 大窗口」—— 大片空白、按钮被顶到
        #    屏幕外。夹到 min/max 之间，脏数据当场失效。
        mn, mx = pre.get("min"), pre.get("max")
        if mn:
            out["w"] = max(out["w"], mn[0])
            out["h"] = max(out["h"], mn[1])
        if mx:
            out["w"] = min(out["w"], mx[0])
            out["h"] = min(out["h"], mx[1])
        else:
            #  没写上限的形态（full）也不能比工作区还大 ——
            #  以前最大化尺寸会被误存进来，下次启动直接顶满屏。
            try:
                wa = win_effects.work_area()
                s0 = win_effects.dpi_scale() or 1.0
                out["w"] = min(out["w"], int(wa["w"] / s0))
                out["h"] = min(out["h"], int(wa["h"] / s0))
            except Exception:
                pass

    #  位置：小浮窗默认右上角；完整形态交给系统居中（不给 x/y）
    pos_key = f"{shape}_pos"
    pos = cfg.get(pos_key)
    if (isinstance(pos, (list, tuple)) and len(pos) == 2
            and all(isinstance(v, int) for v in pos)):
        #  存过的位置是**物理**的，而这里要逻辑的 ——
        #  除以缩放倍率换回去。
        s = win_effects.dpi_scale() or 1.0
        out["x"] = int(round(pos[0] / s)) if s > 1.01 else int(pos[0])
        out["y"] = int(round(pos[1] / s)) if s > 1.01 else int(pos[1])
    elif shape == "compact":
        x, y = win_effects.top_right_position(out["w"], out["h"])
        out["x"], out["y"] = x, y

    return out


def _apply_window_effects(window, wcfg: dict) -> None:
    """窗口显示之后，把毛玻璃 / 圆角 / 置顶这些配上。

    必须在 ``shown`` 之后 —— 那时才有稳定的句柄。
    任何一项失败都只是少一个效果，不影响使用，所以全程兜住异常。
    """
    try:
        from app import win_effects
    except Exception as e:
        log.debug("窗体效果模块不可用：%s", e)
        return

    import time as _t
    _t.sleep(0.7)               # 等 WebView2 画出首帧

    try:
        r = win_effects.setup(
            window,
            glass=bool(wcfg.get("glass")),
            rounded=True,
            on_top=bool(wcfg.get("on_top")),
            dark_titlebar=False,
            shadow=True,
        )
        g = r.get("glass")
        _set_glass_status(bool(g and g.get("ok")))
        if g and not g.get("ok"):
            log.info("毛玻璃没生效（%s），界面会退回不透明背景",
                     g.get("why"))
    except Exception as e:
        log.warning("窗体效果设置失败：%s", e)


#  毛玻璃到底成没成 —— 前端要拿它决定「给不给半透明底」。
#  不透明窗口配半透明背景 = 看起来像蒙了层灰，不如直接给纯色。
_GLASS_OK = False


def _set_glass_status(ok: bool) -> None:
    global _GLASS_OK
    _GLASS_OK = bool(ok)


def _glass_status() -> bool:
    return _GLASS_OK


def main() -> int:
    #  ══════════ 静默预热模式（开机自启走这里）══════════
    #   exe 被开机启动项调用时带 --warmup：
    #     不开窗口、不弹任何东西，后台登录 + 抓一次就退出。
    #   独立成一条路径的原因：这是打包版唯一需要的「第二种用法」，
    #   放在主流程里会让窗口逻辑和后台逻辑互相干扰。
    if "--warmup" in sys.argv or "--quiet" in sys.argv:
        return _run_warmup_only()

    #  卸载时由安装包调用：清掉开机自启的快捷方式。
    #   不做的话，卸载后每次开机都会报「找不到文件」。
    if "--uninstall-cleanup" in sys.argv:
        r = autostart_uninstall()
        log.info("卸载清理：开机自启已移除（%s）", r)
        return 0

    #  ══════════ 命令行指定形态 ══════════
    #  开机自启可以带 --compact 直接以小浮窗开。
    #  写在偏好文件里也能达到同样效果，但命令行更直接 ——
    #  用户从「启动」文件夹里能一眼看见自己配的是哪种。
    if "--compact" in sys.argv:
        cfg = _load_window_cfg()
        cfg["shape"] = "compact"
        cfg["pin_right"] = True
        _save_window_cfg(cfg)
    if "--full" in sys.argv:
        cfg = _load_window_cfg()
        cfg["shape"] = "full"
        _save_window_cfg(cfg)
    if "--no-glass" in sys.argv:
        cfg = _load_window_cfg()
        cfg["glass"] = False
        _save_window_cfg(cfg)

    config.ensure_dirs()
    log.info("=" * 52)
    log.info("启动 ManageBac-packer（解释器 %s）", sys.executable)

    # ★ 启动前先确认 WebView2 在。不在就给提示 + 打开下载页，
    #   而不是让它抛一个用户看不懂的异常然后静默退出。
    if not _webview2_installed():
        log.error("未检测到 WebView2 Runtime，无法启动界面")
        _show_missing_webview2()
        return 3

    # 先用缓存渲染，界面秒开
    pipeline.load_from_cache()

    api = Api()
    #  启动自检：确保 js_api 没有「公开的非函数属性」，
    #   否则 pywebview 会递归遍历 → 窗口「未响应」。
    _check_js_api_safe(api)

    #  ══════════ 窗口形态（v0.5.0）══════════
    #  两种形态：
    #    完整 = 现在的样子，七个标签页
    #    小浮窗 = 一张小卡片，只显示关键信息，默认贴右上角
    #  形态与外观偏好存在 data/window.json，下次启动照旧。
    wcfg = _load_window_cfg()
    shape = wcfg.get("shape") or "full"
    sizes = _window_sizes(shape)

    #  ★ 透明 + 无边框是毛玻璃的前提：
    #    窗口自己不透明的话，DWM 再怎么设也透不出背后的东西。
    want_glass = bool(wcfg.get("glass"))

    #  ★★ 但「无边框」要等前端把标题栏做出来才能开。
    #     踩过的坑：后端先开了 frameless，前端还没画标题栏，
    #     结果窗口既没有系统边框、也没有自绘按钮，
    #     连拖都不能拖 —— 用户看到的就是
    #     「白屏、点不动、像卡死」。
    cfg_frameless = bool(wcfg.get("frameless"))
    if not FRONTEND_HAS_TITLEBAR:
        cfg_frameless = False

    #  ══════════ 拖动机制（无边框窗口靠它）
    #  ★ 0.5.1 起：拖动是程序自己实现的（app.js selfDrag →
    #    win_drag → win_effects.move_by → SetWindowPos）。
    #
    #    pywebview 内建的两套都试过、都阵亡了：
    #      easy_drag=True            —— 全页面误拖，不可用
    #      .pywebview-drag-region    —— 这条链路（内建 http 分发 +
    #        无边框 + WinForms）上它的 move 回调每次都抛
    #        ctypes.ArgumentError（winforms.py:635 把 None 传给
    #        SetWindowPos），窗口纹丝不动，日志刷屏。
    #
    #    另一个教训：webview.settings 是 ImmutableDict，
    #    当年那句 ``webview.settings["DRAG_REGION_DIRECT_TARGET_ONLY"]=True``
    #    其实从来没写进去过 —— 写设置要趁早查类型。
    #  把建窗参数记进日志 —— 白屏那类问题全靠这行排查。
    #  之前没记，只能猜「到底是无边框没生效还是前端挂了」。
    log.info("建窗参数：形态=%s 尺寸=%sx%s 位置=(%s,%s) 无边框=%s "
             "毛玻璃=%s 置顶=%s",
             shape, sizes["w"], sizes["h"],
             sizes.get("x"), sizes.get("y"),
             cfg_frameless, want_glass, bool(wcfg.get("on_top")))

    window = webview.create_window(
        TITLE,
        url=str(config.WEB_DIR / "index.html"),
        js_api=api,
        width=sizes["w"], height=sizes["h"],
        #  ★ pywebview 只认 min_size，**没有 max_size** 这个参数
        #    （我一开始按直觉写了 max_size，会直接 TypeError）。
        min_size=sizes["min"],
        x=sizes.get("x"), y=sizes.get("y"),
        resizable=True,
        frameless=cfg_frameless,
        #  固定 False：拖动由前端 selfDrag 实现（见上面说明）。
        easy_drag=False,
        on_top=bool(wcfg.get("on_top")),
        transparent=want_glass,
        text_select=True,
        #  ★★ background_color 必须给**合法的 6 位十六进制**。
        #     传 None 会让 pywebview 内部
        #         re.match(valid_color, None)
        #     抛 TypeError: expected string or bytes-like object
        #     —— 建窗直接崩，界面根本没机会出现。
        #     想要透明就靠 transparent=True；
        #     Windows 那边会用 Color.Transparent 覆盖掉这个底色，
        #     所以这里给什么色都不影响透明效果。
        background_color="#f5f1e8",
    )
    api._window = window
    api._window_cfg = wcfg
    api._shape = shape

    def after_start() -> None:
        #  窗体效果要在窗口显示之后配 —— 那时句柄才稳定。
        _apply_window_effects(window, wcfg)

        #  ══════════ 双击打开默认最大化（0.5.1，用户要求）══════════
        #  完整形态：打开就铺满屏幕，不用再点一下最大化。
        #  小浮窗除外 —— 浮窗的意义就是「一小块」，最大化就荒谬了。
        #  _apply_window_effects 里已经等了 0.7 秒，窗口画出来了再最大化，
        #  顺序反了会闪一下小窗再弹大。
        if shape == "full":
            try:
                from app import win_effects as _we
                ok = _we.maximize(window)
                log.info("启动即最大化（形态=full）：%s",
                         "已最大化" if ok else "没成（不影响使用）")
            except Exception as e:
                log.debug("启动最大化失败（不影响使用）：%s", e)

        #  ══════════ 无边框安全网 ══════════
        #  无边框窗口没有系统标题栏，关窗全靠界面自己画的按钮。
        #  前端要是没跑起来，窗口就成了关不掉的方块
        #  （用户只能强杀进程 —— 这个坑已经踩过一次）。
        #
        #  所以：等 READY_TIMEOUT 秒，前端还没调 win_ready()，
        #  就主动把系统边框装回去，至少留一个能点的关闭键。
        if cfg_frameless:
            def watch_ready() -> None:
                for _ in range(int(READY_TIMEOUT * 2)):
                    time.sleep(0.5)
                    if getattr(api, "_front_ready", False):
                        return
                #  超时了 —— 前端没报到
                log.warning("前端 %.0f 秒内没报到，把系统边框装回去"
                            "（免得窗口关不掉）", READY_TIMEOUT)
                try:
                    #  恢复边框 = 把 FormBorderStyle 设回 Sizable
                    from System.Windows.Forms import (  # type: ignore
                        FormBorderStyle)
                    window.native.FormBorderStyle = getattr(
                        FormBorderStyle, "Sizable")
                    window.set_title(TITLE)
                except Exception as e:
                    log.warning("装回边框失败：%s", e)

            threading.Thread(target=watch_ready, daemon=True).start()

        # 启动自动刷新循环（每 config.AUTO_REFRESH_SEC 秒抓一次）
        pipeline.start_auto_refresh()

        def work() -> None:
            import time as _t

            # 等界面渲染完，用户已经看到缓存内容
            _t.sleep(0.8)

            #  只抓一次。以前这里是 refresh_async() + ensure_fresh()，
            #   两个都会抓 → 抢锁 + 双重请求 → 容易触发限速，冷启动像卡死。
            #   现在统一交给一个「开机准备」函数：
            #     - 缓存够新 → 直接返回（不碰网络）
            #     - 需要抓   → 自己抓一次（含自动登录）
            try:
                from app import warmup

                warmup.startup_prepare(verbose=False)
            except Exception as e:
                log.warning("开机准备失败，退回普通刷新：%s", e)
                try:
                    pipeline.refresh_async()
                except Exception:
                    pass

        threading.Thread(target=work, daemon=True).start()

    # ══════════════════════════════════════════════════════════════
    #  让 WebView2 的存储**跨启动保留**
    #
    #  ★★ 这一条是实测出来的，不是照文档写的。
    #
    #     pywebview 的 `private_mode` **默认是 True**，而它的实现是：
    #
    #         if not _state['private_mode'] or _state['storage_path']:
    #             cache_dir = _state['storage_path'] or .../pywebview
    #         else:
    #             cache_dir = tempfile.TemporaryDirectory().name   # ← 每次都是新的
    #
    #     也就是说，隐私模式下每次启动都拿一个**全新的临时目录**，
    #     于是 localStorage 每次都是空的。
    #
    #     实测（`_tools/probe_localstorage.py` 连起两次窗口）：
    #         阶段1 读到的 probe = null   →  GONE
    #         阶段2 读到的 probe = null   →  GONE
    #
    #  ★ 影响面比想象的大：
    #     界面上有 34 处在用 localStorage —— 主题、配色、背景图、
    #     字体颜色、窗口形态、页签位置、教程的「看过了」……
    #     这​​些原来**全部在每次启动时归零**。
    #     （「教程每次启动都弹出来」就是这个问题显出来的样子。）
    #
    #  ★ 为什么以前没这么写：
    #     `_archive/diag/test_storage.py` 里查过「指定 storage_path
    #     会不会导致 WebView2 初始化卡死」——怕引入卡死就没改。
    #     现在实测确认不会：`_tools/probe_persist.py` 两次都是
    #     5 秒内正常退出，第二次正确读到第一次写的值（KEEP）。
    #
    #  ★ 目录放在 config.DATA_DIR 下面，跟着其它数据一起走
    #     （打包版在 %LOCALAPPDATA%，不在只读的安装目录里）。
    # ══════════════════════════════════════════════════════════════
    _wv_storage = None
    try:
        _wv_storage = config.DATA_DIR / "webview2"
        _wv_storage.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        #  目录建不出来就算了 —— 退回原来的行为（本次启动能用，
        #  只是设置不保留），总比起不来好。
        log.warning("WebView2 存储目录准备失败，本次设置不会保留：%s", e)
        _wv_storage = None

    if _wv_storage:
        webview.start(after_start, debug=False,
                      private_mode=False,
                      storage_path=str(_wv_storage))
    else:
        webview.start(after_start, debug=False)

    log.info("正常退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
