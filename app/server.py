"""本地网页版看板 —— 用标准库实现的极简 HTTP 服务。

    GET /                  看板页面（web/index.html）
    GET /api/data          当前数据快照（JSON）
    GET /api/refresh       立即触发一次抓取
    GET /api/auto?on=1|0   开关自动刷新
    GET /api/open?url=...  用系统默认浏览器打开链接
    GET /api/health        健康检查

不依赖任何第三方 Web 框架，只用 http.server。
"""
from __future__ import annotations

import json
import logging
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config, pipeline

log = logging.getLogger(__name__)


class Handler(BaseHTTPRequestHandler):
    server_version = "MBWidget/1.0"

    # ---------------- 工具 ----------------

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, data, code: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    # ---------------- 路由 ----------------

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)

        try:
            if path == "/":
                return self._serve_index()
            if path == "/api/data":
                return self._json(pipeline.snapshot())
            if path == "/api/health":
                return self._json({"ok": True})
            if path == "/api/refresh":
                pipeline.refresh_async()
                return self._json({"ok": True})
            if path == "/api/auto":
                val = (query.get("on", ["1"])[0] or "1")
                on = val not in ("0", "false", "off", "no")
                pipeline.set_auto_refresh(on)
                return self._json({"ok": True, "enabled": on})
            if path == "/api/open":
                url = (query.get("url", [""])[0] or "").strip()
                if not url:
                    return self._json({"ok": False, "error": "缺少 url"}, 400)
                try:
                    webbrowser.open(url, new=2)
                    return self._json({"ok": True})
                except Exception as e:
                    return self._json({"ok": False, "error": str(e)}, 500)

            # ---------- 下载 ----------
            if path == "/api/download_task":
                cid = (query.get("cid", [""])[0] or "").strip()
                tid = (query.get("tid", [""])[0] or "").strip()
                title = (query.get("title", [""])[0] or "").strip()
                if not cid or not tid:
                    return self._json({"ok": False, "error": "缺少 cid/tid"}, 400)
                from . import download as dl

                res = dl.download_task(cid, tid, title)
                if res.get("ok") and res.get("folder"):
                    pass    # 不自动开文件夹，避免打断
                return self._json(res)

            if path == "/api/download_files":
                cid = (query.get("cid", [""])[0] or "").strip()
                name = (query.get("name", [""])[0] or "").strip()
                if not cid:
                    return self._json({"ok": False, "error": "缺少 cid"}, 400)
                from . import download as dl

                return self._json(dl.download_files(cid, name))

            if path == "/api/files":
                cid = (query.get("cid", [""])[0] or "").strip()
                folder = (query.get("folder", [""])[0] or "").strip() or None
                if not cid:
                    return self._json({"ok": False, "error": "缺少 cid"}, 400)
                from . import download as dl

                items = dl.list_files(cid, folder)
                return self._json({"ok": True, "items": items,
                                   "folder": folder or ""})

            if path == "/api/open_folder":
                from . import download as dl

                dl.open_folder((query.get("path", [""])[0] or "").strip())
                return self._json({"ok": True})

            return self._json({"error": "not found", "path": path}, 404)
        except Exception as e:
            log.exception("请求处理失败 %s", path)
            return self._json({"error": str(e)}, 500)

    def _serve_index(self) -> None:
        f = config.WEB_DIR / "index.html"
        if not f.exists():
            return self._send(500, b"index.html missing", "text/plain; charset=utf-8")
        body = f.read_bytes()
        self._send(200, body, "text/html; charset=utf-8")

    def log_message(self, *args) -> None:
        pass          # 静音，避免刷屏


class DashboardServer:
    """可后台运行的看板服务。"""

    def __init__(self, port: int = 8765, host: str = "127.0.0.1") -> None:
        self.host = host
        self.port = port
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> int:
        """启动服务，返回实际使用的端口（端口被占用时自动 +1）。"""
        last_err: Exception | None = None
        for p in range(self.port, self.port + 12):
            try:
                httpd = ThreadingHTTPServer((self.host, p), Handler)
                httpd.daemon_threads = True
                self._httpd = httpd
                self.port = p
                break
            except OSError as e:
                last_err = e
                continue
        if self._httpd is None:
            raise RuntimeError(f"无法绑定端口：{last_err}")

        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        log.info("看板服务已启动：http://%s:%d", self.host, self.port)
        return self.port

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    def stop(self) -> None:
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:
                pass
        self._httpd = None
