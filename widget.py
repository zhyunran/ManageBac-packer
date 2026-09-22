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
            log.info("js_api 自检通过（无公开的非函数属性）")
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
        """前端问「需要引导吗？」

         只看本地有没有填过账号，不发网络请求 ——
          这样界面一打开就能立刻决定显示引导页还是主界面。
        """
        return {
            "need_setup": not config.has_credentials(),
            "data_dir": str(config.ROOT),
            "frozen": bool(getattr(sys, "frozen", False)),
        }

    def save_setup(self, login: str, password: str, url: str = "",
                   schedule_login: str = "", schedule_password: str = ""):
        """保存账号密码（引导页提交）。

         只写「真正需要的键」，不覆盖已有的其他配置。
         写完立刻用新凭据试一次登录，让用户当场知道对不对 ——
          不然他以为填好了，其实要等半小时后才发现是错的。
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
            log.warning("重载凭据失败（重启后生效）：%s", e)

        log.info("凭据已保存到 %s", path)
        return {"ok": True, "path": str(path)}

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

    def teams_sync(self, rounds=8):
        """同步一次（往回翻 → 读页面 → 分类 → 存本地）。

        rounds 是往回翻的轮数：
          8  = 默认，翻到 Teams 不再加载为止（通常够用）
          25 = 「多翻一点」，往更深处翻（会慢一些）

        ★ 放在后台线程里跑，界面不会卡住。
        """
        if getattr(self, "_teams_syncing", False):
            return {"ok": True, "already": True}
        self._teams_syncing = True

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
        self._tm_crawling = True

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
        """下载 EC 消息里的附件，并尝试提取 PDF 文字。"""
        try:
            from app import teams
            data = teams.load()
            ec = data.get("ec") or []
            urls = []
            for it in ec:
                for a in (it.get("attachments") or []):
                    u = teams.safe_url(a.get("url") or "")
                    if u and all(x["url"] != u for x in urls):
                        urls.append({"url": u, "name": a.get("name") or "attach.pdf"})
            if not urls:
                return {"ok": False, "reason": "EC 消息里还没有附件（先同步一次）"}

            from app import httpclient
            teams.EC_DIR.mkdir(parents=True, exist_ok=True)
            got, texts = 0, 0
            for item in urls[:teams.MAX_ATTACH]:
                try:
                    dest = teams.EC_DIR / item["name"][:120]
                    ok = httpclient.download_direct(item["url"], dest)
                    if not ok:
                        continue
                    got += 1
                    if dest.suffix.lower() == ".pdf":
                        r = teams.extract_pdf_text(dest)
                        if r.get("ok"):
                            texts += 1
                            # 把提取到的文字挂回对应的 EC 记录
                            for it in ec:
                                for a in (it.get("attachments") or []):
                                    if a.get("url") == item["url"]:
                                        it["file_text"] = r["text"][:20000]
                                        it["file_note"] = r.get("note") or ""
                except Exception as e:
                    log.debug("下载 EC 附件失败：%s", e)

            data["ec"] = ec
            teams.save(data)
            return {"ok": True, "downloaded": got, "text_extracted": texts,
                    "total": len(urls), "dir": str(teams.EC_DIR)}
        except Exception as e:
            log.warning("下载 EC 附件失败：%s", e)
            return {"ok": False, "reason": str(e)}

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
    window = webview.create_window(TITLE,
        url=str(config.WEB_DIR / "index.html"),
        js_api=api,
        width=W, height=H, min_size=(370, 500),
        resizable=True, easy_drag=False, text_select=True,
        background_color="#f5f1e8",
    )
    api._window = window

    def after_start() -> None:
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

    webview.start(after_start, debug=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
