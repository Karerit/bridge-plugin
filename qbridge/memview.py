"""记忆查看页：让管理员在浏览器里看到所有会话的记忆。

**为什么不用 AstrBot 自带的插件可视化视图**（``views/`` + ``context.register_web_api``）：
那套机制是较新版本才有的（``astrbot/api/web.py`` 在 4.22 及以前不存在，``plugin_page_service``
到 4.28 才齐），而本插件声明支持 ``>=4.5.7``。所以这里自带一个极小的 HTTP 服务，
不依赖 AstrBot 版本，端口默认 8899。

**安全默认值**（都在 ``memview`` 配置组里）：

  · 默认只监听 ``127.0.0.1``，只有本机能打开；
  · 改成非回环地址（如 ``0.0.0.0``）时必须设置 token，否则拒绝启动——一个能被全网读到的
    记忆库不叫「管理员可见」，叫「所有人可见」；
  · 删除默认关闭（``memview.allowDelete``）。

只做「读快照 + 可选删除」，记忆数据的读写语义留在插件里（通过回调注入），
这样这个模块可以脱离 AstrBot 单独测试。单线程 asyncio 处理，不引入新依赖。
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlsplit

logger = logging.getLogger("astrbot_plugin_qq_bridge")

MAX_HEADER_BYTES = 16 * 1024
MAX_BODY_BYTES = 256 * 1024
READ_TIMEOUT = 15.0
LOOPBACK_NAMES = {"127.0.0.1", "::1", "localhost", "localhost.localdomain"}

STATUS_TEXT = {
    200: "OK",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    413: "Payload Too Large",
    431: "Request Header Fields Too Large",
    500: "Internal Server Error",
}


def is_loopback(host: str) -> bool:
    """判断监听地址是否只对本机开放。"""
    text = (host or "").strip().lower()
    if text in LOOPBACK_NAMES:
        return True
    return text.startswith("127.") or text.startswith("[::1]")


class MemoryView:
    """记忆查看页的 HTTP 服务。

    回调由插件注入：
      snapshot_fn() -> dict                 整份记忆快照（原样 JSON 返回）
      delete_fn(umo, index) -> bool         删除某会话的第 index 条
      clear_fn(umo) -> bool                 清空某会话
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8899,
        token: str = "",
        allow_delete: bool = False,
        page_path: Path | None = None,
        snapshot_fn: Callable[[], dict] | None = None,
        delete_fn: Callable[[str, int], bool] | None = None,
        clear_fn: Callable[[str], bool] | None = None,
    ) -> None:
        self.host = (host or "127.0.0.1").strip()
        self.port = int(port or 8899)
        self.token = (token or "").strip()
        self.allow_delete = bool(allow_delete)
        self.page_path = Path(page_path) if page_path else None
        self._snapshot_fn = snapshot_fn
        self._delete_fn = delete_fn
        self._clear_fn = clear_fn
        self._server: asyncio.AbstractServer | None = None
        self.last_error = ""
        self._empty_requests = 0
        self._noted_reasons: set[str] = set()

    # ── 生命周期 ──────────────────────────────────────────────────────────────
    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def empty_requests(self) -> int:
        """收到过多少次「连上但没发请求」的连接。

        这个数字是排查 ERR_EMPTY_RESPONSE 的关键：浏览器的请求如果压根没到达插件
        （容器/WSL 网络命名空间、代理、防火墙），这里会一直是 0。
        """
        return self._empty_requests

    def url(self, token: bool = True) -> str:
        """给管理员看的地址。绑定 0.0.0.0 时按本机地址显示。"""
        host = self.host
        if host in ("0.0.0.0", "::", ""):
            host = "127.0.0.1"
        query = f"?token={self.token}" if (token and self.token) else ""
        return f"http://{host}:{self.port}/{query}"

    async def start(self) -> bool:
        """起服务。已在跑返回 True；起不来返回 False 并记录原因。"""
        if self.running:
            return True
        if not is_loopback(self.host) and not self.token:
            self.last_error = "监听地址不是回环地址，但没设置 token；为避免记忆被全网读到，已拒绝启动"
            logger.error(f"[qq-bridge] 记忆查看页未启动：{self.last_error}（在配置里设 memview.token）")
            return False
        try:
            self._server = await asyncio.start_server(self._handle, self.host, self.port)
        except OSError as exc:
            self.last_error = f"端口 {self.port} 监听失败：{exc}"
            logger.error(f"[qq-bridge] 记忆查看页未启动：{self.last_error}")
            return False
        except Exception as exc:  # 兜底：不让查看页拖垮插件
            self.last_error = f"启动异常：{exc}"
            logger.error(f"[qq-bridge] 记忆查看页未启动：{self.last_error}")
            return False
        self.last_error = ""
        logger.info(
            f"[qq-bridge] 记忆查看页已启动：{self.url()}"
            f"（{'允许删除' if self.allow_delete else '只读'}）"
        )
        return True

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is None:
            return
        server.close()
        try:
            await server.wait_closed()
        except Exception as exc:
            logger.debug(f"[qq-bridge] 关闭记忆查看页时出错：{exc}")

    # ── 请求处理 ──────────────────────────────────────────────────────────────
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await self._serve(reader, writer)
        except Exception as exc:  # 单个请求出错不影响服务
            logger.debug(f"[qq-bridge] 记忆查看页处理请求失败：{exc}")
            try:
                await self._send(writer, 500, "text/plain; charset=utf-8", b"internal error")
            except Exception:
                pass
        finally:
            # 关之前先等缓冲写出去。少了这一步，浏览器可能「连上了却一个字节都没收到」
            # （ERR_EMPTY_RESPONSE），在 Windows 的 Proactor 事件循环上尤其明显。
            try:
                await writer.drain()
            except Exception:
                pass
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=READ_TIMEOUT)
        except asyncio.TimeoutError:
            self._note_no_request("请求头读取超时（连上了但一直没发请求）")
            await self._send(writer, 408, "text/plain; charset=utf-8", "请求超时。".encode())
            return
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, OSError) as exc:
            # 浏览器的预连接/复用连接会在不发任何数据的情况下关掉连接，属正常现象。
            # 仍然回一个状态码：万一不是预连接，用户看到的是 400 而不是「空白页」。
            self._note_no_request(f"连接在读请求前就结束：{type(exc).__name__}")
            await self._send(writer, 400, "text/plain; charset=utf-8", "请求不完整。".encode())
            return
        if len(head) > MAX_HEADER_BYTES:
            await self._send(writer, 431, "text/plain; charset=utf-8", b"header too large")
            return

        text = head.decode("latin-1")
        lines = text.split("\r\n")
        parts = (lines[0].split(" ") + ["", ""])[:3]
        method, target = parts[0].upper(), parts[1]
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                key, _, value = line.partition(":")
                headers[key.strip().lower()] = value.strip()

        try:
            need = int(headers.get("content-length") or 0)
        except ValueError:
            need = 0
        if need > MAX_BODY_BYTES:
            await self._send(writer, 413, "text/plain; charset=utf-8", b"body too large")
            return
        body = b""
        while len(body) < need:
            chunk = await reader.read(need - len(body))
            if not chunk:
                break
            body += chunk

        split = urlsplit(target)
        path = (split.path or "/").rstrip("/") or "/"
        query = parse_qs(split.query)

        if self.token and not self._authorized(query, headers):
            await self._send(writer, 401, "text/plain; charset=utf-8", "需要 token。".encode())
            return

        if path == "/" or path == "/index.html":
            if method != "GET":
                await self._send(writer, 405, "text/plain; charset=utf-8", b"method not allowed")
                return
            await self._send_html(writer)
        elif path == "/healthz":
            await self._send(writer, 200, "text/plain; charset=utf-8", b"ok")
        elif path == "/api/memory":
            if method != "GET":
                await self._send(writer, 405, "text/plain; charset=utf-8", b"method not allowed")
                return
            await self._send_json(writer, self._snapshot())
        elif path == "/api/memory/export":
            if method != "GET":
                await self._send(writer, 405, "text/plain; charset=utf-8", b"method not allowed")
                return
            payload = json.dumps(self._snapshot(), ensure_ascii=False, indent=2).encode("utf-8")
            await self._send(
                writer,
                200,
                "application/json; charset=utf-8",
                payload,
                {"Content-Disposition": 'attachment; filename="qq-bridge-memory.json"'},
            )
        elif path == "/api/memory/delete":
            if method != "POST":
                await self._send(writer, 405, "text/plain; charset=utf-8", b"method not allowed")
                return
            await self._handle_delete(writer, body)
        else:
            await self._send(writer, 404, "text/plain; charset=utf-8", b"not found")

    # ── 路由实现 ──────────────────────────────────────────────────────────────
    def _snapshot(self) -> dict:
        if self._snapshot_fn is None:
            return {"sessions": [], "totalSessions": 0, "totalItems": 0}
        data = self._snapshot_fn()
        return data if isinstance(data, dict) else {"sessions": []}

    async def _handle_delete(self, writer: asyncio.StreamWriter, body: bytes) -> None:
        if not self.allow_delete:
            await self._send_json(writer, {"error": "删除已关闭（memview.allowDelete）"}, status=403)
            return
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            await self._send_json(writer, {"error": "请求体不是合法 JSON"}, status=400)
            return
        umo = str(payload.get("umo") or "")
        if not umo:
            await self._send_json(writer, {"error": "缺少 umo"}, status=400)
            return
        if payload.get("all"):
            ok = bool(self._clear_fn(umo)) if self._clear_fn else False
            await self._send_json(writer, {"ok": ok, "cleared": umo}, status=200 if ok else 500)
            return
        try:
            index = int(payload.get("index"))
        except (TypeError, ValueError):
            await self._send_json(writer, {"error": "index 必须是整数"}, status=400)
            return
        ok = bool(self._delete_fn(umo, index)) if self._delete_fn else False
        await self._send_json(writer, {"ok": ok, "umo": umo, "index": index}, status=200 if ok else 500)

    def _note_no_request(self, reason: str) -> None:
        """记一次「连上但没发请求」。浏览器预连接很常见，所以每个原因只告警一次。"""
        self._empty_requests += 1
        if reason not in self._noted_reasons:
            self._noted_reasons.add(reason)
            logger.warning(
                f"[qq-bridge] 记忆查看页收到一个没有内容的连接（{reason}）。"
                f"浏览器预连接属正常；若页面打不开（ERR_EMPTY_RESPONSE），"
                f"请确认 AstrBot 与浏览器在同一台机器/同一网络命名空间——"
                f"容器或 WSL 里只监听 127.0.0.1 是访问不到的"
            )

    def _authorized(self, query: dict, headers: dict) -> bool:
        given = (query.get("token") or [""])[0] or headers.get("x-qqbridge-token", "")
        if not given:
            cookie = headers.get("cookie", "")
            for chunk in cookie.split(";"):
                name, _, value = chunk.strip().partition("=")
                if name == "qqbridge_token":
                    given = value
                    break
        return bool(given) and hmac.compare_digest(str(given), self.token)

    async def _send_html(self, writer: asyncio.StreamWriter) -> None:
        html = ""
        if self.page_path and self.page_path.is_file():
            try:
                html = self.page_path.read_text(encoding="utf-8")
            except OSError as exc:
                logger.debug(f"[qq-bridge] 读记忆查看页失败：{exc}")
        if not html:
            html = "<!doctype html><meta charset='utf-8'><h3>记忆查看页文件缺失</h3>"
        # 页面里用 __QQBRIDGE_TOKEN__ 占位，服务端替换，避免把 token 拼进前端脚本
        html = html.replace("__QQBRIDGE_TOKEN__", self.token)
        await self._send(writer, 200, "text/html; charset=utf-8", html.encode("utf-8"))

    async def _send_json(self, writer: asyncio.StreamWriter, data: dict, status: int = 200) -> None:
        await self._send(
            writer,
            status,
            "application/json; charset=utf-8",
            json.dumps(data, ensure_ascii=False).encode("utf-8"),
        )

    async def _send(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        content_type: str,
        body: bytes,
        extra: dict | None = None,
    ) -> None:
        head = [
            f"HTTP/1.1 {status} {STATUS_TEXT.get(status, 'OK')}",
            f"Content-Type: {content_type}",
            f"Content-Length: {len(body)}",
            "Cache-Control: no-store",
            "X-Content-Type-Options: nosniff",
            "Referrer-Policy: no-referrer",
            "Connection: close",
        ]
        for key, value in (extra or {}).items():
            head.append(f"{key}: {value}")
        writer.write(("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + body)
        await writer.drain()
