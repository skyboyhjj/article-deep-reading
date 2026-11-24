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
import re
import socket
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

import asr
import codes
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
# 文案里显示的是 round(260000/1024) = 254 KB，别拿「260」去核对——
# 那只是取整前的近似，两个数都对，说的是不同东西。
BODY_LIMIT = 260_000
# 超限时最多额外读掉多少再回复：够覆盖正常误操作，又不给恶意大包留带宽。
DRAIN_MAX = 5 * 1024 * 1024

# 版本号：/api/health 与 SSE 首帧都带上，用户复制的诊断信息里就有它。
# 发版时改这一处，CHANGELOG 首个标题必须同号（test_ui_contract.TestVersion 会比对，防漂）。
APP_VERSION = "2.33"

# 上传：用户自己导出视频号视频后传上来，是拿到「原声」的唯一可靠路径
UPLOAD_DIR = Path(tempfile.gettempdir()) / "deepread_uploads"
ALLOWED_EXT = {".mp4", ".mov", ".m4v", ".m4a", ".mp3", ".wav", ".aac",
               ".flac", ".webm", ".mkv", ".ogg", ".amr", ".opus", ".silk"}
MAX_UPLOAD = int(os.environ.get("MAX_UPLOAD_MB", "300")) * 1024 * 1024
CHUNK = 256 * 1024
# 上传件的保留期。分析结束会立刻删；这条是兜底 —— 上传后没点「开始读解」的，
# 最多留这么久。见 _sweep_uploads()。
UPLOAD_TTL = int(os.environ.get("UPLOAD_TTL_HOURS", "24")) * 3600

# 从混杂文本里挑链接：用户常把「推荐阅读：https://… 这篇不错」整段粘进来。
# 只认带协议的 http(s)，避免把普通域名/邮箱误当链接。
_URL_RE = re.compile(r"https?://[^\s<>\"'“”‘’（）\[\]【】]+", re.I)
_URL_TAIL = "。，、；：！？）》」』”’.,;:!?]>}"


def extract_url(raw) -> str:
    """取第一个链接；纯链接原样返回，取不到返回空串。"""
    text = str(raw or "").strip()
    m = _URL_RE.search(text)
    if not m:
        return text if text.startswith(("http://", "https://")) else ""
    url = m.group(0).rstrip(_URL_TAIL)
    # 括号保留在链接里（维基式 URL），只剥「多出来的」右括号，如 "见（…）" 的收尾
    while url.endswith(")") and url.count(")") > url.count("("):
        url = url[:-1]
    return url


def _sweep_uploads(now: float | None = None) -> int:
    """删掉超过 UPLOAD_TTL 的上传件，返回删除个数。

    惰性清扫：每次上传前跑一次，不起后台线程。多副本各自扫自己的 /tmp，互不干扰；
    没有上传活动时也不会有堆积 —— 因为写入前必扫。
    """
    now = time.time() if now is None else now
    n = 0
    try:
        entries = list(UPLOAD_DIR.iterdir())
    except OSError:          # 目录还不存在（首次运行）
        return 0
    for f in entries:
        try:
            if f.is_file() and now - f.stat().st_mtime > UPLOAD_TTL:
                f.unlink(missing_ok=True)
                n += 1
        except OSError:      # 单个别文件删不掉（权限/竞态）不该影响整体清扫
            pass
    return n

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

    # ---------- 追踪号与结构化日志 ----------
    def _mark(self) -> None:
        """请求入口打点，供日志算耗时。"""
        self._t0 = time.time()

    def _rid(self) -> str:
        """每请求一个 8 位追踪号。用户报障时截图这一串，我们据此翻日志。"""
        rid = getattr(self, "_request_id", None)
        if rid is None:
            rid = self._request_id = uuid.uuid4().hex[:8]
        return rid

    def _log_error(self, status: int, err: str | None = None, exc: str | None = None) -> None:
        """一行式结构化日志。只记元信息，不记正文——与页面上的隐私承诺一致。

        `exc` 是内部异常摘要（类型 + 消息），只进日志、绝不上页面。
        """
        should = status >= 500
        if err is not None:
            should = codes.get(err).log
        if not should:
            return
        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "rid": self._rid(),
            "code": err,
            "http": status,
            "path": urlsplit(self.path).path,
            "ms": int((time.time() - getattr(self, "_t0", time.time())) * 1000),
            "ip": ratelimit._hash_ip(self._client_ip())[:8],
        }
        if exc:
            rec["exc"] = exc
        print(json.dumps(rec, ensure_ascii=False), flush=True)

    # ---------- 基础 ----------
    def _send(self, code: int, body: bytes, ctype: str, headers: dict | None = None,
              write_body: bool = True) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Request-Id", self._rid())
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; script-src 'unsafe-inline'; "
                         "style-src 'unsafe-inline'; connect-src 'self'; "
                         "img-src 'self' data:; font-src 'self'; "
                         "base-uri 'none'; form-action 'self'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if write_body:
            self.wfile.write(body)

    def _json(self, code: int, obj: dict, headers: dict | None = None,
              write_body: bool = True, err: str | None = None,
              exc: str | None = None) -> None:
        """JSON 返回。`err` 传错误码，决定要不要写日志；`exc` 只进日志。"""
        if code >= 400:
            self._log_error(code, err, exc)
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(),
                   "application/json; charset=utf-8", headers, write_body=write_body)

    def _fail(self, key: str, headers: dict | None = None, write_body: bool = True,
              retry_in_seconds: float | None = None, log_exc: str | None = None,
              **fmt) -> None:
        """统一错误出口。码表决定状态码与文案，这里只负责拼装与发送。

        - `fmt`：文案里的占位符（如 limit / minutes / ext / options）
        - `retry_in_seconds`：写进 body，供前端倒计时；HTTP 头 Retry-After 由调用方放 headers
        - `log_exc`：内部异常摘要，只进日志，不上页面
        """
        c = codes.get(key)
        body = codes.payload(key, **fmt)
        body["ok"] = False
        body["request_id"] = self._rid()
        body["retry_in_seconds"] = retry_in_seconds
        self._json(c.http, body, headers, write_body=write_body, err=key, exc=log_exc)

    def _exceeded(self, retry_after: float | None) -> None:
        """额度用尽：429 + Retry-After，让客户端能明确告知「约 X 后恢复」。"""
        sec = int(round(retry_after)) if retry_after else 0
        self._fail("DR-3001", headers={"Retry-After": str(sec)},
                   retry_in_seconds=sec, limit=LIMITER.current_limit(),
                   minutes=max(1, round(sec / 60)))

    def _enrich_fail(self, ev: dict) -> str | None:
        """流中 fail 帧补齐码表字段，与流前 JSON 用同一句话。返回内部异常摘要。

        分工是这样的：pipeline 负责「挂在哪个阶段、是哪个码、异常是什么」，
        文案与分级一律从码表取。异常摘要通过 `log_exc` 传给日志，**先摘掉再上帧**——
        页面上一个字都不会露。

        为什么要这么绕：v2.31 之前 pipeline 的帧 text 里直接拼 `{exc}`，
        靠 app 覆盖 text 来兜底。那一层依赖太脆——换个人加个新出口就漏了。
        现在两头都干净：源码里没有 exc 上页面的路径，app 再从帧里摘一次。
        """
        key = ev.get("code") or "DR-5003"
        try:
            fields = codes.payload(key)
        except KeyError:            # 未登记的码（pipeline 写了码表里没有的）
            key = "DR-5003"
            fields = codes.payload(key)
        exc = ev.pop("log_exc", None)      # 先摘，任何分支都不会把它发出去
        # 与流前 JSON 走同一个 payload：字段集一个字不差，改口径只改一处
        ev.update(fields)
        ev["code"] = key
        ev["request_id"] = self._rid()
        # text 留给不认识 error 字段的老页面；内容仍是人话
        ev["text"] = f"{ev['error']}。{ev['hint']}"
        return exc

    # ---------- SSE ----------
    def _sse_open(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")  # 让 nginx 不缓冲
        self.send_header("X-Request-Id", self._rid())
        self.send_header("Access-Control-Allow-Origin", "*")
        # 不用 Transfer-Encoding: chunked：该头在 HTTP/2 下被禁止，nginx 反代时
        # 会触发 ERR_HTTP2_PROTOCOL_ERROR。改用 Connection: close，响应结束于连接关闭。
        self.send_header("Connection", "close")
        self.end_headers()

    def _sse(self, obj: dict) -> None:
        data = ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode()
        self.wfile.write(data)
        self.wfile.flush()

    def _sse_close(self) -> None:
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
        self._fail("DR-3002")
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
            self._fail("DR-2001")
            return
        if size > MAX_UPLOAD:
            self._fail("DR-2002", limit=MAX_UPLOAD // 1024 // 1024)
            return

        raw_name = unquote(self.headers.get("X-Filename") or "upload")
        ext = media.ext_of(raw_name)
        if ext not in ALLOWED_EXT:
            self._fail("DR-2003", ext=ext or "(无扩展名)",
                       options=", ".join(sorted(ALLOWED_EXT)))
            return
        name = media.safe_filename(raw_name, ext=ext)

        ok, left, state, retry_after = LIMITER.allow(self._client_ip())
        if not ok:
            if state == "unavailable":
                self._fail("DR-3003")
            else:
                self._exceeded(retry_after)
            return

        _sweep_uploads()        # 惰性清扫：趁这次上传顺手清掉过期的，不额外起线程
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
            self._fail("DR-5002", log_exc=f"{type(exc).__name__}: {exc}")
            return

        if written != size:
            dst.unlink(missing_ok=True)
            self._fail("DR-2006")
            return

        self._json(200, {
            "id": uid, "name": name, "bytes": written,
            "duration": round(media.probe_duration(dst), 1),
            "asr": asr.describe(),
            "remaining": left,
        })

    def _handle_drop(self) -> None:
        """删掉一个上传件。幂等：文件本来就不在，也回 200（removed: 0）。

        为什么不消耗额度：删除是收尾动作，收费会让人宁可留着不删。
        为什么不占并发槽：删一个文件是毫秒级操作，占槽只会白白挤掉分析任务。
        """
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            self._fail("DR-1001")
            return
        if n > 4096:
            self._fail("DR-1002", limit="4 KB", advice="重新提交")
            return
        try:
            payload = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            self._fail("DR-1003")
            return
        uid = str(payload.get("id", "")).strip()
        if not (uid.isalnum() and len(uid) == 16):
            self._fail("DR-2004")
            return
        removed = 0
        for f in sorted(UPLOAD_DIR.glob(f"{uid}__*")):
            try:
                f.unlink(missing_ok=True)
                removed += 1
            except OSError:
                pass
        self._json(200, {"removed": removed})

    # ---------- 路由 ----------
    def do_GET(self) -> None:  # noqa: N802
        self._mark()
        self._route_get(write_body=True)

    def do_HEAD(self) -> None:  # noqa: N802
        """HEAD 与 GET 同头、无 body。分享卡片抓取常先 HEAD 探 og:image，此前 501 会挡掉预览。"""
        self._mark()
        self._route_get(write_body=False)

    def _route_get(self, write_body: bool) -> None:
        path = urlsplit(self.path).path
        if path in ("/", "/index.html"):
            try:
                body = (WEB / "index.html").read_bytes()
            except OSError:
                self._fail("DR-5001", write_body=write_body)
                return
            self._send(200, body, "text/html; charset=utf-8", write_body=write_body)
            return
        if path == "/og.jpg":
            try:
                body = (WEB / "og.jpg").read_bytes()
            except OSError:
                self._fail("DR-1007", write_body=write_body)
                return
            self._send(200, body, "image/jpeg", write_body=write_body)
            return
        if path == "/api/health":
            self._json(200, {
                "ok": True,
                "version": APP_VERSION,
                "llm": llm.is_configured(),
                "asr": asr.is_configured(),
                "asr_backend": asr.describe(),
                "search": search.is_configured(),
                "search_backend": search.describe(),
                "ffmpeg": media.has_ffmpeg(),
                "max_upload_mb": MAX_UPLOAD // 1024 // 1024,
                "rate_limit_per_hour": LIMITER.current_limit(),
                "rate_limit_backend": type(LIMITER).__name__,
                "max_concurrent": MAX_CONCURRENT,
            }, write_body=write_body)
            return
        self._fail("DR-1007", write_body=write_body)

    def do_POST(self) -> None:  # noqa: N802
        self._mark()
        path = urlsplit(self.path).path
        if path == "/api/upload":
            if not self._acquire_slot():
                return
            try:
                self._handle_upload()
            finally:
                _slots.release()
            return
        if path == "/api/upload/drop":
            # 删掉自己刚上传的文件。不消耗额度（删除不该收费），也不占并发槽。
            self._handle_drop()
            return
        if path != "/api/analyze":
            self._fail("DR-1007")
            return

        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            self._fail("DR-1001")
            return
        if n > BODY_LIMIT:
            # 260 000 字节是防内存的硬闸（文案里显示 round/1024 = 254 KB，
            # 两个数都对，说的是不同东西——别按「260」去核对文案）。
            # 中文一字 3 字节，所以实际能粘的汉字远少于字符上限，
            # 这里直接把真实可用量写出来，别让用户对着「20 万字符」白贴一遍。
            if n <= DRAIN_MAX:      # 常规超限：读完再回，客户端才收得到这条提示
                self._drain(n)
            self._fail("DR-1002", limit=f"{round(BODY_LIMIT / 1024)} KB，中文约 8.6 万字",
                       advice="长文请分段粘贴")
            return
        try:
            payload = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            self._fail("DR-1003")
            return

        url = extract_url(payload.get("url", ""))
        pasted = str(payload.get("text", ""))
        ptitle = str(payload.get("title", ""))
        upload_id = str(payload.get("upload_id", "")).strip()

        media_path, media_name = "", ""
        if upload_id:
            if not upload_id.isalnum():
                self._fail("DR-2004")
                return
            hits = sorted(UPLOAD_DIR.glob(f"{upload_id}__*"))
            if not hits:
                self._fail("DR-2005")
                return
            media_path = str(hits[0])
            media_name = hits[0].name.split("__", 1)[-1]

        if not pasted and not upload_id and not url.startswith(("http://", "https://")):
            self._fail("DR-1004")
            return
        if len(pasted) > 200_000:
            self._fail("DR-1006")
            return
        # 正文不足 200 字、又没有链接或音视频时，pipeline 也只会回「没有可用输入」。
        # 与其扣掉额度再报错，不如进 pipeline 前就 400（阈值与 pipeline 的正文判定一致）。
        if pasted and not upload_id and not url.startswith(("http://", "https://")) and len(pasted) < 200:
            self._fail("DR-1005")
            return

        ok, left, state, retry_after = LIMITER.allow(self._client_ip())
        if not ok:
            if state == "unavailable":
                self._fail("DR-3003")
            else:
                self._exceeded(retry_after)
            return

        if not self._acquire_slot():
            return
        try:
            self._sse_open()
            self._sse({"stage": "quota", "status": "ok", "text": f"本小时剩余 {left} 次",
                       "request_id": self._rid(), "version": APP_VERSION})
            completed = False
            try:
                for ev in pipeline.analyze(url, pasted, ptitle, media_path, media_name):
                    if ev.get("status") == "fail":
                        # 流中错误：帧里补齐码表字段（文案 / 分级 / 追踪号），
                        # 异常摘要只进日志，并按码表决定是否写日志
                        exc = self._enrich_fail(ev)
                        self._log_error(200, ev["code"], exc)
                    # 「读完」的判据是收到 done 帧，不是循环没抛异常。
                    # pipeline 的业务失败是 yield fail 后 return —— 对这里而言是正常结束，
                    # 若据此判 completed，失败的音视频会在 finally 里被删掉，用户想重试就得重传。
                    if ev.get("stage") == "done":
                        completed = True
                    self._sse(ev)
            except BrokenPipeError:
                return  # 客户端断开（不算读完，保留上传件以便重试）
            except Exception as exc:  # noqa: BLE001
                try:
                    self._log_error(200, "DR-5003", f"{type(exc).__name__}: {exc}")
                    frame = {"stage": "error", "status": "fail", "code": "DR-5003"}
                    self._enrich_fail(frame)
                    self._sse(frame)
                except OSError:
                    return
            finally:
                try:
                    self._sse_close()
                except OSError:
                    pass
                if media_path and completed:
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

    # 启动先扫一次：/tmp 不保证跨重启清空（裸机不会），旧文件不该跟着重启一起留下
    _swept = _sweep_uploads()
    if _swept:
        print(f"启动清扫：删除 {_swept} 个过期上传件", flush=True)

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
