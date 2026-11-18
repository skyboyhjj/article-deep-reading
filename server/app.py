"""HTTP 服务（标准库，零框架依赖）。

选标准库而不是 FastAPI，是为了让 clone 下来的人一条命令就能跑：
    export LLM_API_KEY=... && python3 server/app.py
生产部署再套 nginx / Caddy 反代即可。

路由：
    GET  /              单页前端
    GET  /api/health    {"ok": true, "llm": 是否已配置}
    POST /api/analyze   {"url": "..."} → SSE 流（每阶段一个事件）
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import socket
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

import asr
import llm
import media
import pipeline
import ratelimit
import search

HERE = Path(__file__).resolve().parent
WEB = HERE.parent / "web"

# 极简限流：每 IP 每小时 N 次（防滥用；真实场景请换 Redis + 更细策略）
RATE_LIMIT = int(os.environ.get("RATE_LIMIT_PER_HOUR", "10"))


def _parse_trusted(raw: str) -> tuple[frozenset[str], tuple]:
    """解析 TRUSTED_PROXIES：支持逗号分隔的 IP 与 CIDR 网段。

    支持网段是必要的 —— Docker 容器重建后内网 IP 会变，
    只写单个 IP 的话，一次重启就悄悄失效（退化成「全站共享额度」）。
    """
    exact: set[str] = set()
    nets: list = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "/" in part:
            try:
                nets.append(ipaddress.ip_network(part, strict=False))
            except ValueError:
                pass
        else:
            exact.add(part)
    return frozenset(exact), tuple(nets)

# /api/analyze 的请求体上限（字节）。中文一字 3 字节，故约 8.6 万汉字。
BODY_LIMIT = 260_000
# 超限时最多额外读掉多少再回复：够覆盖正常误操作，又不给恶意大包留带宽。
DRAIN_MAX = 5 * 1024 * 1024

# 上传：用户自己导出视频号视频后传上来，是拿到「原声」的唯一可靠路径
UPLOAD_DIR = Path(tempfile.gettempdir()) / "deepread_uploads"
ALLOWED_EXT = {".mp4", ".mov", ".m4v", ".m4a", ".mp3", ".wav", ".aac",
               ".flac", ".webm", ".mkv", ".ogg", ".amr", ".opus", ".silk"}
MAX_UPLOAD = int(os.environ.get("MAX_UPLOAD_MB", "300")) * 1024 * 1024
CHUNK = 256 * 1024

# 限流：内存（单机/开发）或 Redis（多副本部署），由 REDIS_URL 决定。
# 注意「没配 Redis」是模式，「Redis 连不上」是故障 —— 后者 fail closed 回 503。
LIMITER = ratelimit.build_limiter(RATE_LIMIT)

# 反代后取真实 IP：只有直连 peer 落在可信代理名单里，才采纳 X-Real-IP。
# 默认**不信任任何头** —— 宁可漏配导致「全站共享额度」（功能故障，立刻看得见），
# 也不要默认全信导致「谁都能伪造头绕过限流」（安全漏洞，没人会察觉）。
_TRUST_EXACT, _TRUST_NETS = _parse_trusted(os.environ.get("TRUSTED_PROXIES", ""))

# 同时进行的任务上限。一次读解要跑 1–2 分钟，ThreadingHTTPServer 默认线程无上限，
# 高峰期会把整机拖垮（见 docs/ARCHITECTURE.md §三末）。
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "8"))
# 等额度的秒数。等不到就 503 —— 不做无上限排队，否则空线程会堆满。
SLOT_WAIT = float(os.environ.get("SLOT_WAIT_SEC", "20"))
# 用 Bounded 版：release 多于 acquire 会立刻报错，能当场抓出漏配对的分支。
_slots = threading.BoundedSemaphore(MAX_CONCURRENT)


def _peer_trusted(peer: str) -> bool:
    if peer in _TRUST_EXACT:
        return True
    if not _TRUST_NETS:
        return False
    try:
        addr = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return any(addr in net for net in _TRUST_NETS)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "DeepRead/0.1"

    def log_message(self, fmt: str, *args) -> None:  # 静音默认日志
        pass

    # ---------- 基础 ----------
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: dict) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    # ---------- SSE ----------
    def _sse_open(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")  # 让 nginx 不缓冲
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def _sse(self, obj: dict) -> None:
        data = ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode()
        self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
        self.wfile.flush()

    def _sse_close(self) -> None:
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    # ---------- 客户端 IP ----------
    def _client_ip(self) -> str:
        """取真实客户端 IP。

        反代后 `client_address` 是 nginx 容器的内网 IP；不改这一行，
        限流会把全站算成同一个人（第一个访客用满额度，所有人 429）。
        只在直连 peer 属于可信代理时才采信 X-Real-IP。
        """
        peer = self.client_address[0]
        if not (_TRUST_EXACT or _TRUST_NETS):
            return peer                       # 未配置可信代理 = 不信任任何头
        if not _peer_trusted(peer):
            return peer
        real = (self.headers.get("X-Real-IP") or "").strip()
        if not real:
            fwd = (self.headers.get("X-Forwarded-For") or "").split(",")
            real = fwd[-1].strip() if fwd else ""   # 取最后一跳，第一跳可被客户端伪造
        return real or peer

    # ---------- 并发额度 ----------
    def _acquire_slot(self) -> bool:
        """占一个并发额度。等不到就 503 并主动断开。

        不无限等：ThreadingHTTPServer 每连接一线程，让它们卡在信号量上
        等于把线程数问题换个地方堆着。断开让客户端尽早重试。
        """
        if _slots.acquire(timeout=SLOT_WAIT):
            return True
        self.close_connection = True
        self._json(503, {"error": f"同时进行的任务已达 {MAX_CONCURRENT} 个，请稍后重试"})
        return False

    # ---------- 上传 ----------
    def _drain(self, n: int) -> None:
        """把请求体读掉丢弃。

        超限时不读就回复，残留字节会被当成下一个请求解析（HTTP/1.1 keep-alive），
        客户端也可能先吃到连接重置、看不到那句错误文案。
        """
        while n > 0:
            chunk = self.rfile.read(min(CHUNK, n))
            if not chunk:
                break
            n -= len(chunk)

    def _handle_upload(self) -> None:
        """裸字节流上传（Content-Type: application/octet-stream）。

        不用 multipart：标准库解析 multipart 在 3.12 已无 cgi 模块可依，
        自己拼解析器反而更脆。前端读文件后按流发，简单可靠。
        """
        try:
            size = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            size = 0
        if size <= 0:
            self._json(400, {"error": "缺少 Content-Length"})
            return
        if size > MAX_UPLOAD:
            self._json(413, {"error": f"文件超过上限 {MAX_UPLOAD // 1024 // 1024} MB"})
            return

        raw_name = unquote(self.headers.get("X-Filename") or "upload")
        name = media.safe_filename(raw_name)
        ext = Path(name).suffix.lower()
        if ext not in ALLOWED_EXT:
            self._json(400, {"error": f"不支持的文件类型 {ext or '(无扩展名)'}；"
                                      f"可选：{', '.join(sorted(ALLOWED_EXT))}"})
            return

        ok, left, state = LIMITER.allow(self._client_ip())
        if not ok:
            if state == "unavailable":
                self._json(503, {"error": "限流服务暂不可用，请稍后再试"})
            else:
                self._json(429, {"error": f"每小时限 {RATE_LIMIT} 次，请稍后再试"})
            return

        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        uid = uuid.uuid4().hex[:16]
        dst = UPLOAD_DIR / f"{uid}__{name}"
        written = 0
        try:
            with open(dst, "wb") as fh:
                while written < size:
                    chunk = self.rfile.read(min(CHUNK, size - written))
                    if not chunk:
                        break
                    fh.write(chunk)
                    written += len(chunk)
        except OSError as exc:
            dst.unlink(missing_ok=True)
            self._json(500, {"error": f"写入失败：{exc}"})
            return

        if written != size:
            dst.unlink(missing_ok=True)
            self._json(400, {"error": f"传输不完整（{written}/{size} 字节）"})
            return

        self._json(200, {
            "id": uid, "name": name, "bytes": written,
            "duration": round(media.probe_duration(dst), 1),
            "asr": asr.describe(),
            "remaining": left,
        })

    # ---------- 路由 ----------
    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/index.html"):
            try:
                body = (WEB / "index.html").read_bytes()
            except OSError:
                self._json(500, {"error": "前端文件缺失"})
                return
            self._send(200, body, "text/html; charset=utf-8")
            return
        if self.path == "/api/health":
            self._json(200, {
                "ok": True,
                "llm": llm.is_configured(),
                "asr": asr.is_configured(),
                "asr_backend": asr.describe(),
                "search": search.is_configured(),
                "search_backend": search.describe(),
                "ffmpeg": media.has_ffmpeg(),
                "max_upload_mb": MAX_UPLOAD // 1024 // 1024,
                "rate_limit_per_hour": RATE_LIMIT,
                "rate_limit_backend": type(LIMITER).__name__,
                "max_concurrent": MAX_CONCURRENT,
            })
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/api/upload":
            if not self._acquire_slot():
                return
            try:
                self._handle_upload()
            finally:
                _slots.release()
            return
        if self.path != "/api/analyze":
            self._json(404, {"error": "not found"})
            return

        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            self._json(400, {"error": "请求体缺失"})
            return
        if n > BODY_LIMIT:
            # 260 KB 是防内存的硬闸。中文一字 3 字节，所以实际能粘的汉字远少于字符上限，
            # 这里直接把真实可用量写出来，别让用户对着「20 万字符」白贴一遍。
            if n <= DRAIN_MAX:      # 常规超限：读完再回，客户端才收得到这条提示
                self._drain(n)
            self._json(400, {"error": f"请求体过大（上限 {round(BODY_LIMIT / 1024)} KB，"
                                      "中文约 8.6 万汉字）；长文请分段粘贴"})
            return
        try:
            payload = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "JSON 解析失败"})
            return

        url = str(payload.get("url", "")).strip()
        pasted = str(payload.get("text", ""))
        ptitle = str(payload.get("title", ""))
        upload_id = str(payload.get("upload_id", "")).strip()

        media_path, media_name = "", ""
        if upload_id:
            if not upload_id.isalnum():
                self._json(400, {"error": "upload_id 非法"})
                return
            hits = sorted(UPLOAD_DIR.glob(f"{upload_id}__*"))
            if not hits:
                self._json(404, {"error": "上传件不存在或已过期，请重新上传"})
                return
            media_path = str(hits[0])
            media_name = hits[0].name.split("__", 1)[-1]

        if not pasted and not upload_id and not url.startswith(("http://", "https://")):
            self._json(400, {"error": "请提供链接、粘贴正文，或上传音视频"})
            return
        if len(pasted) > 200_000:
            self._json(400, {"error": "粘贴正文过长（上限 20 万字符）"})
            return

        ok, left, state = LIMITER.allow(self._client_ip())
        if not ok:
            if state == "unavailable":
                self._json(503, {"error": "限流服务暂不可用，请稍后再试"})
            else:
                self._json(429, {"error": f"每小时限 {RATE_LIMIT} 次，请稍后再试"})
            return

        if not self._acquire_slot():
            return
        try:
            self._sse_open()
            self._sse({"stage": "quota", "status": "ok", "text": f"本小时剩余 {left} 次"})
            try:
                for ev in pipeline.analyze(url, pasted, ptitle, media_path, media_name):
                    self._sse(ev)
            except BrokenPipeError:
                return  # 客户端断开
            except Exception as exc:  # noqa: BLE001
                try:
                    self._sse({"stage": "error", "status": "fail",
                               "text": f"服务异常：{type(exc).__name__}: {exc}"})
                except OSError:
                    return
            finally:
                try:
                    self._sse_close()
                except OSError:
                    pass
                if media_path:
                    Path(media_path).unlink(missing_ok=True)
        finally:
            _slots.release()


def _lan_ip() -> str:
    """取本机局域网地址，方便手机同 WiFi 直接访问。取不到就返回空串。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()


def main() -> None:
    """优先级：命令行参数 > 环境变量 > 内置默认。

    python3 server/app.py                 # 8000
    python3 server/app.py -p 8123         # 8123
    python3 server/app.py --port 8123 --host 127.0.0.1
    """
    ap = argparse.ArgumentParser(
        prog="app.py",
        description="读解服务：抓取文章 → 判体裁 → 核验 → 实算 → 出报告",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python3 server/app.py -p 8123\n"
            "  python3 server/app.py --port 8123 --host 127.0.0.1\n"
            "  PORT=8123 python3 server/app.py\n"
        ),
    )
    ap.add_argument(
        "-p", "--port", type=int, default=int(os.environ.get("PORT", "8000")),
        help="监听端口（默认取环境变量 PORT，仍无则 8000）",
    )
    ap.add_argument(
        "--host", default=os.environ.get("HOST", "0.0.0.0"),
        help="监听地址（默认 0.0.0.0；只想本机访问用 127.0.0.1）",
    )
    args = ap.parse_args()

    if not (1 <= args.port <= 65535):
        ap.error(f"端口必须在 1-65535 之间，收到 {args.port}")

    try:
        srv = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        msg = str(exc)
        if "Address already in use" in msg or getattr(exc, "errno", None) == 98:
            print(f"端口 {args.port} 已被占用。换一个端口，例如：\n"
                  f"  python3 server/app.py -p {args.port + 1}")
        elif getattr(exc, "errno", None) == 13:
            print(f"没有权限绑定端口 {args.port}。1024 以下端口通常需要管理员权限，"
                  f"建议改用 8000 以上。")
        else:
            print(f"启动失败：{msg}")
        raise SystemExit(1)

    srv.daemon_threads = True
    shown = "127.0.0.1" if args.host in ("0.0.0.0", "") else args.host
    print(f"读解服务已启动： http://{shown}:{args.port}", flush=True)
    print(f"监听地址：{args.host}:{args.port}", flush=True)
    if args.host in ("0.0.0.0", ""):
        lan = _lan_ip()
        if lan:
            print(f"手机访问（同一 WiFi）： http://{lan}:{args.port}", flush=True)
    print(f"LLM 配置：{'已就绪' if llm.is_configured() else '未配置（设置 LLM_API_KEY 后可用）'}",
          flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止", flush=True)


if __name__ == "__main__":
    main()
