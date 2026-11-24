#!/usr/bin/env python3
"""HTTP 层的用例：真起一个服务器，打真请求，不联网、不花钱。

此前 app.py 一条测试都没有 —— 而它是最外面那层门：19 个分支（路由、
上传校验、正文上限、限流、SSE 收尾）全靠手工点。门坏了里面再对也没用。

做法：绑定 127.0.0.1 的随机端口起 ThreadingHTTPServer，
用 http.client 发真实请求，每个用例结束清空限流计数。

不测的：真读解（会调模型）。`/api/analyze` 的成功路径用假
`pipeline.analyze` 替换，只验 SSE 的形状与收尾。
"""
import http.client
import io
import socket
import ipaddress
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from contextlib import redirect_stdout
from unittest import mock
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app  # noqa: E402
import ratelimit  # noqa: E402

HOST = "127.0.0.1"
PORT = 0
SERVER = None
THREAD = None


def setUpModule():
    global SERVER, THREAD, PORT
    SERVER = ThreadingHTTPServer((HOST, PORT), app.Handler)
    PORT = SERVER.server_address[1]
    THREAD = threading.Thread(target=SERVER.serve_forever, daemon=True)
    THREAD.start()


def tearDownModule():
    if SERVER:
        SERVER.shutdown()
        SERVER.server_close()


def request(method, path, body=None, headers=None):
    """每个请求开独立连接：被测分支常不读完 body，复用连接会互相污染。"""
    conn = http.client.HTTPConnection(HOST, PORT, timeout=20)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read()
    finally:
        conn.close()


def post_json(path, obj, extra=None):
    body = json.dumps(obj).encode()
    headers = {"Content-Type": "application/json", "Content-Length": str(len(body))}
    headers.update(extra or {})
    return request("POST", path, body=body, headers=headers)


class Base(unittest.TestCase):
    def setUp(self):
        app.LIMITER.reset()
        # 每个用例一个独立的上传目录：共享系统 temp 时，
        # 上次运行残留的文件会让断言时对时错（踩过一次）。
        tmp = Path(tempfile.mkdtemp(prefix="apptest_"))
        patcher = mock.patch.object(app, "UPLOAD_DIR", tmp)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, tmp, True)

    def tearDown(self):
        app.LIMITER.reset()

    def wait_until(self, cond, timeout=2.0, step=0.02):
        """SSE 的结束标记先于服务端收尾动作到达，断言不能建立在「读完即已清」上。"""
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            time.sleep(step)
        return cond()


class TestRequestId(Base):
    """每请求一个追踪号：响应头带出来，用户报障时截图即可对号；5xx 另写一行日志。"""

    def test_header_present_and_hex(self):
        status, headers, _ = request("GET", "/")
        self.assertEqual(status, 200)
        rid = headers.get("X-Request-Id")
        self.assertTrue(rid, "响应头缺 X-Request-Id")
        self.assertRegex(rid, r"^[0-9a-f]{8}$")

    def test_unique_per_request(self):
        _, h1, _ = request("GET", "/")
        _, h2, _ = request("GET", "/")
        self.assertNotEqual(h1.get("X-Request-Id"), h2.get("X-Request-Id"))

    def test_present_on_client_error(self):
        status, headers, _ = post_json("/api/analyze", {"text": "短"})
        self.assertEqual(status, 400)
        self.assertRegex(headers.get("X-Request-Id", ""), r"^[0-9a-f]{8}$")

    def test_no_log_for_user_error(self):
        """输入类错误不写日志——否则日志被「正文太短」刷满，真正要看的 5xx 被淹没。"""
        with mock.patch("builtins.print") as printed:
            post_json("/api/analyze", {"text": "短"})
        lines = [str(c.args[0]) for c in printed.call_args_list if c.args]
        self.assertFalse([l for l in lines if l.startswith("{")], "4xx 输入错误不应写结构化日志")

    def test_log_written_for_server_error(self):
        """5xx 一定写日志，且日志里的 rid 与响应头一致。"""
        with mock.patch.object(app, "WEB", Path("/nonexistent-web")):
            with mock.patch("builtins.print") as printed:
                status, headers, body = request("GET", "/")
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(body)["code"], "DR-5001")
        lines = [str(c.args[0]) for c in printed.call_args_list if c.args]
        recs = [l for l in lines if l.startswith("{")]
        self.assertTrue(recs, "5xx 应写一条 JSON 日志")
        obj = json.loads(recs[0])
        for k in ("ts", "rid", "http", "path", "ms", "ip"):
            self.assertIn(k, obj)
        self.assertEqual(obj["http"], 500)
        self.assertEqual(obj["rid"], headers.get("X-Request-Id"))

    def test_storage_write_failure_is_coded(self):
        """写盘失败要给用户一句人话 + 一个码，原始异常只进日志（不上页面）。"""
        with mock.patch("builtins.open", side_effect=OSError("disk full")):
            with mock.patch("builtins.print"):
                status, headers, body = request(
                    "POST", "/api/upload", body=b"RIFF0000WAVE",
                    headers={"Content-Type": "application/octet-stream",
                             "X-Filename": "a.wav", "Content-Length": "12"})
        self.assertEqual(status, 500)
        obj = json.loads(body)
        self.assertEqual(obj["code"], "DR-5002")
        self.assertNotIn("disk full", obj["error"] + obj["hint"])


class TestErrorShape(Base):
    """错误响应体的字段契约。前端与文档都依赖这些字段名，改动必须过这里。"""

    def test_user_error_body(self):
        status, headers, body = post_json("/api/analyze", {"text": "短"})
        obj = json.loads(body)
        self.assertEqual(status, 400)
        self.assertEqual(obj["code"], "DR-1005")
        self.assertEqual(obj["condition"], "textTooShort")
        self.assertFalse(obj["ok"])
        self.assertFalse(obj["retryable"])
        self.assertIsNone(obj["retry_in_seconds"])
        self.assertTrue(obj["error"])
        self.assertTrue(obj["hint"])
        self.assertEqual(obj["responsibility"], "user")
        # 展示口径由服务端定：输入类不摆码、不摆诊断（前端照着画就行）
        self.assertEqual(obj["level"], "fail")
        self.assertFalse(obj["show_code"])
        self.assertFalse(obj["show_diag"])
        self.assertRegex(obj["request_id"], r"^[0-9a-f]{8}$")
        self.assertEqual(obj["request_id"], headers.get("X-Request-Id"))

    def test_validation_error_carries_code(self):
        """「没有可读解的内容」这类校验错误也要带码，否则前端分不出该改什么。"""
        status, _, body = post_json("/api/analyze", {"url": "", "text": ""})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["code"], "DR-1004")

    def test_unknown_path_has_code(self):
        status, _, body = request("GET", "/api/nothing")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["code"], "DR-1007")

    def test_error_body_has_responsibility(self):
        """前端分色靠 responsibility——三档都得能出得去，前端不必自己抄码表。"""
        status, _, body = post_json("/api/analyze", {"text": "短"})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["responsibility"], "user")

    def test_server_side_error_says_us(self):
        # 页面文件缺失 → 我们的锅。前端据此把码与诊断信息摆到显眼处。
        with mock.patch.object(app, "WEB", Path("/nonexistent-web")):
            status, _, body = request("GET", "/")
        self.assertEqual(status, 500)
        obj = json.loads(body)
        self.assertEqual(obj["code"], "DR-5001")
        self.assertEqual(obj["responsibility"], "us")
        self.assertEqual(obj["level"], "fail")       # 我们的锅：红
        self.assertTrue(obj["show_diag"])            # 且必须给诊断信息


class TestGet(Base):
    def test_index_served(self):
        status, headers, body = request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertGreater(len(body), 500)

    def test_security_headers(self):
        status, headers, _ = request("GET", "/")
        self.assertEqual(status, 200)
        csp = headers.get("Content-Security-Policy", "")
        self.assertIn("default-src 'none'", csp)
        self.assertIn("script-src 'unsafe-inline'", csp)
        self.assertEqual(headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(headers.get("Referrer-Policy"), "no-referrer")
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        self.assertIn("camera=()", headers.get("Permissions-Policy", ""))

    def test_index_html_alias(self):
        status, _, body = request("GET", "/index.html")
        self.assertEqual(status, 200)
        self.assertEqual(body, request("GET", "/")[2])

    def test_og_image_served(self):
        # og:image 分享卡片图片必须能被公开访问（评估报告 4.2）。
        status, headers, body = request("GET", "/og.jpg")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "image/jpeg")
        self.assertEqual(body[:3], b"\xff\xd8\xff")

    def test_head_og_image(self):
        # HEAD /og.jpg：分享卡片抓取常先 HEAD 探 og:image，此前 501 会挡掉预览。
        status, headers, body = request("HEAD", "/og.jpg")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "image/jpeg")
        self.assertGreater(int(headers.get("Content-Length")), 0)
        self.assertEqual(body, b"")   # HEAD 无 body

    def test_health_shape_and_no_secret(self):
        os.environ["LLM_API_KEY"] = "sk-should-never-appear"
        try:
            status, _, body = request("GET", "/api/health")
        finally:
            os.environ.pop("LLM_API_KEY", None)
        self.assertEqual(status, 200)
        data = json.loads(body)
        for key in ("ok", "version", "llm", "asr", "asr_backend", "search", "search_backend",
                    "ffmpeg", "max_upload_mb", "rate_limit_per_hour", "max_concurrent"):
            self.assertIn(key, data)
        self.assertNotIn(b"sk-should-never-appear", body)

    def test_unknown_path_404(self):
        status, _, body = request("GET", "/nope")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["code"], "DR-1007")

    def test_query_string_stripped(self):
        # 分享链接常带 utm / ref 等参数，路径匹配必须忽略 ? 之后的部分，
        # 否则整条分享链路第一步就 404（评估报告 4.1）。
        for path in ("/?utm_source=test", "/?from=share", "/index.html?ref=wx",
                     "/api/health?x=1"):
            status, _, _ = request("GET", path)
            self.assertEqual(status, 200, f"GET {path} 应返回 200，实为 {status}")


class TestAnalyzeValidation(Base):
    """这些分支都在进入 pipeline 之前返回，所以不花钱。"""

    def test_empty_body(self):
        status, _, body = request("POST", "/api/analyze", body=b"",
                                  headers={"Content-Length": "0"})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["code"], "DR-1001")

    def test_oversize_body(self):
        payload = b"x" * 260_001
        status, _, body = request("POST", "/api/analyze", body=payload,
                                  headers={"Content-Length": str(len(payload))})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["code"], "DR-1002")

    def test_oversize_does_not_poison_next_request(self):
        """超限时若把 body 读完再回，同一连接上的下一个请求才不会解析错乱。"""
        conn = http.client.HTTPConnection(HOST, PORT, timeout=20)
        try:
            payload = b"x" * 260_001
            conn.request("POST", "/api/analyze", body=payload,
                         headers={"Content-Length": str(len(payload))})
            first = conn.getresponse()
            first.read()
            self.assertEqual(first.status, 400)

            conn.request("GET", "/api/health")
            second = conn.getresponse()
            self.assertEqual(second.status, 200)
            self.assertTrue(json.loads(second.read())["ok"])
        finally:
            conn.close()

    def test_bad_json(self):
        status, _, body = request("POST", "/api/analyze", body=b"{not json",
                                  headers={"Content-Length": "9"})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["code"], "DR-1003")

    def test_upload_id_not_alnum(self):
        status, _, body = post_json("/api/analyze", {"upload_id": "../../etc"})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["code"], "DR-2004")

    def test_upload_id_missing_on_disk(self):
        status, _, body = post_json("/api/analyze", {"upload_id": "deadbeef0000"})
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["code"], "DR-2005")

    def test_nothing_provided(self):
        status, _, body = post_json("/api/analyze", {"url": "ftp://x/y"})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["code"], "DR-1004")

    def test_pasted_text_too_long(self):
        """用 ASCII 才测得到这条分支：20 万字符 = 20 万字节，未撞 26 万字节的体量上限。"""
        status, _, body = post_json("/api/analyze", {"text": "x" * 200_001})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["code"], "DR-1006")

    def test_pasted_text_too_short(self):
        """正文不足 200 字、又没链接/音视频时，进 pipeline 前就该 400，不能白扣额度。"""
        status, _, body = post_json("/api/analyze", {"text": "太短了"})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["code"], "DR-1005")

    def test_chinese_hits_body_limit_first(self):
        """中文 20 万字符 = 60 万字节，先撞 260 KB 那道闸。

        两道限制在中文下并不重叠：字符上限（20 万）永远走不到，
        实际可用量由字节闸决定 —— 约 8.6 万汉字。
        所以那条错误文案必须把这个数说出来，否则用户会对着
        「上限 20 万字符」白贴一遍。v2.8 已改。
        """
        status, _, body = post_json("/api/analyze", {"text": "字" * 200_001})
        self.assertEqual(status, 400)
        obj = json.loads(body)
        # 撞的是字节闸（DR-1002），不是字符闸（DR-1006）——中文 20 万字符 = 60 万字节
        self.assertEqual(obj["code"], "DR-1002")
        # 验「上限数字真的说了出来」，但绑常量不绑文案：文案可改，常量不该漂
        kb = f"{round(app.BODY_LIMIT / 1024)} KB"
        self.assertIn(kb, obj["error"], "上限数字没落进文案，用户会对着「20 万字符」白贴一遍")
        self.assertNotIn("{", obj["error"] + obj["hint"], "占位符没被替换，用户会看到花括号")

    def test_unknown_post_path(self):
        status, _, _ = post_json("/api/nope", {})
        self.assertEqual(status, 404)


class TestAnalyzeStream(Base):
    def _fake(self, *events):
        def gen(url, pasted, title, media_path, media_name):
            for ev in events:
                yield ev
        return gen

    def test_sse_stream_shape(self):
        events = [{"stage": "fetch", "status": "run", "text": "抓取中"},
                  {"stage": "done", "status": "ok", "text": "完成"}]
        with mock.patch.object(app.pipeline, "analyze", self._fake(*events)):
            status, headers, body = post_json("/api/analyze", {"url": "https://example.com/a"})
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", headers["Content-Type"])
        self.assertEqual(headers.get("X-Accel-Buffering"), "no")
        text = body.decode()
        self.assertIn('"stage": "quota"', text)          # 先报配额
        self.assertIn('"stage": "fetch"', text)
        self.assertIn('"stage": "done"', text)
        self.assertTrue(text.endswith("\n\n"))           # 收尾干净
        # 首帧带版本与追踪号：前端不必另发请求就能拼出诊断信息（设计 §4.2）
        quota = json.loads(text.split("\n\n")[0][5:].strip())
        self.assertEqual(quota["stage"], "quota")
        self.assertEqual(quota["version"], app.APP_VERSION)
        self.assertRegex(quota["request_id"], r"^[0-9a-f]{8}$")

    def test_stream_fail_frame_is_enriched(self):
        """流中 fail 帧补齐码表字段；帧里的内部异常摘要不许上页面。"""
        ev = {"stage": "triage", "status": "fail", "code": "DR-4002",
              "text": "模型调用失败：ConnectionError: secret-internal-host"}
        with mock.patch.object(app.pipeline, "analyze", self._fake(ev)):
            status, _, body = post_json("/api/analyze", {"url": "https://example.com/e"})
        self.assertEqual(status, 200)
        frames = [json.loads(c[5:].strip()) for c in body.decode().split("\n\n")
                  if c.startswith("data:")]
        fail = [f for f in frames if f.get("status") == "fail"][0]
        self.assertEqual(fail["code"], "DR-4002")
        self.assertEqual(fail["responsibility"], "third")
        self.assertEqual(fail["level"], "warn")        # 外部依赖：黄
        self.assertTrue(fail["show_diag"])
        self.assertEqual(fail["condition"], "modelFailed")
        self.assertTrue(fail["retryable"])
        self.assertTrue(fail["hint"])
        self.assertRegex(fail["request_id"], r"^[0-9a-f]{8}$")
        self.assertNotIn("secret-internal-host", fail["text"])

    def test_pipeline_exception_becomes_error_event(self):
        def boom(*a, **k):
            raise RuntimeError("模型挂了")
            yield  # 让它成为生成器
        with mock.patch.object(app.pipeline, "analyze", boom):
            status, _, body = post_json("/api/analyze", {"url": "https://example.com/b"})
        self.assertEqual(status, 200)
        text = body.decode()
        self.assertIn('"stage": "error"', text)
        self.assertIn('"code": "DR-5003"', text)
        self.assertIn("服务异常", text)
        self.assertNotIn("模型挂了", text)   # 内部细节只进日志，不上页面

    def test_stream_fail_logs_internal_summary(self):
        """异常摘要的两头：页面上不许有，日志里必须有。

        只做到「不上页面」还不够——把摘要彻底丢掉，排障时只剩一个码。
        所以帧带 `log_exc`，app 摘下来喂日志，帧本身不带出去。
        """
        ev = {"stage": "triage", "status": "fail", "code": "DR-4002",
              "log_exc": "LLMError: timeout after 60s"}
        buf = io.StringIO()
        with mock.patch.object(app.pipeline, "analyze", self._fake(ev)), \
                redirect_stdout(buf):
            status, _, body = post_json("/api/analyze", {"url": "https://example.com/f"})
        self.assertEqual(status, 200)
        page = body.decode()
        self.assertNotIn("timeout after 60s", page)          # 不上页面
        self.assertNotIn("log_exc", page)                    # 连字段名都不出去
        self.assertIn("timeout after 60s", buf.getvalue())   # 但日志里要有

    def test_rate_limit_blocks_second_call(self):
        with mock.patch.object(app, "LIMITER", ratelimit.MemoryRateLimiter(1)), \
                mock.patch.object(app.pipeline, "analyze", self._fake({"stage": "done"})):
            first, _, _ = post_json("/api/analyze", {"url": "https://example.com/c"})
            second, headers, body = post_json("/api/analyze", {"url": "https://example.com/c"})
        self.assertEqual(first, 200)
        self.assertEqual(second, 429)
        obj = json.loads(body)
        self.assertEqual(obj["code"], "DR-3001")
        # Retry-After（头，给非 JS 客户端）与 retry_in_seconds（体，给前端倒计时）必须一致
        self.assertEqual(obj["retry_in_seconds"], int(headers.get("Retry-After", "-1")))

    def test_analyze_deletes_temp_media(self):
        """上传件用完即删 —— 不删会在 temp 里攒一堆视频。

        注意时序：客户端读到 `0\\r\\n\\r\\n` 时，服务端可能还没执行到 finally
        里的 unlink（实测约 5% 概率）。所以这里等条件成立，而不是读完就断言。
        """
        app.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        fake = app.UPLOAD_DIR / "abc12345__v.mp4"
        fake.write_bytes(b"\0" * 32)
        try:
            with mock.patch.object(app.pipeline, "analyze", self._fake({"stage": "done"})):
                status, _, _ = post_json("/api/analyze", {"upload_id": "abc12345"})
            self.assertEqual(status, 200)
            self.assertTrue(self.wait_until(lambda: not fake.exists()),
                            "analyze 结束后应删除临时上传件")
        finally:
            fake.unlink(missing_ok=True)

    def test_analyze_keeps_temp_media_on_pipeline_error(self):
        """分析没正常跑完（模型异常/断流）不该删上传件 —— 删了用户就没法重试。

        断流（BrokenPipeError）走 return、模型异常走 except，两条路径都到 finally
        但 completed 为 False，都不该删。这里用抛异常模拟；真断流在单测里难以稳定构造。
        """
        app.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        fake = app.UPLOAD_DIR / "def67890__v.mp4"
        fake.write_bytes(b"\0" * 32)
        try:
            def boom(*a, **k):
                raise RuntimeError("模型挂了")
                yield  # 让它成为生成器
            with mock.patch.object(app.pipeline, "analyze", boom):
                status, _, body = post_json("/api/analyze", {"upload_id": "def67890"})
            self.assertEqual(status, 200)
            self.assertIn("服务异常", body.decode())
            self.assertTrue(fake.exists(),
                            "分析异常时应保留上传件，供用户重试")
        finally:
            fake.unlink(missing_ok=True)


class TestExtractUrl(unittest.TestCase):
    """输入框允许整段文字，服务端也要能从里面认出链接。"""

    def test_plain_url_unchanged(self):
        u = "https://mp.weixin.qq.com/s/abc123"
        self.assertEqual(app.extract_url(u), u)

    def test_extracts_from_surrounding_text(self):
        raw = "推荐阅读：https://mp.weixin.qq.com/s/abc123 这篇不错"
        self.assertEqual(app.extract_url(raw), "https://mp.weixin.qq.com/s/abc123")

    def test_strips_trailing_cjk_punctuation(self):
        raw = "看这个 https://mp.weixin.qq.com/s/abc123。"
        self.assertEqual(app.extract_url(raw), "https://mp.weixin.qq.com/s/abc123")

    def test_no_url_returns_empty(self):
        self.assertEqual(app.extract_url("只有一段文字，没有链接"), "")

    def test_bare_domain_not_treated_as_url(self):
        self.assertEqual(app.extract_url("mp.weixin.qq.com/s/abc"), "")

    def test_keeps_balanced_parentheses(self):
        u = "https://zh.wikipedia.org/wiki/Foo_(bar)"
        self.assertEqual(app.extract_url(f"（见 {u}）"), u)

    def test_drops_unbalanced_trailing_paren(self):
        self.assertEqual(app.extract_url("(见 https://a.com/x)"), "https://a.com/x")


class TestUpload(Base):
    def test_missing_content_length(self):
        status, _, body = request("POST", "/api/upload", body=b"",
                                  headers={"X-Filename": "a.mp4"})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["code"], "DR-2001")

    def test_truncated_upload_is_coded(self):
        """声明 100 字节只发 10 字节：读到 EOF 就该报「上传不完整」，不能静默收下。"""
        sock = socket.create_connection((HOST, PORT), timeout=10)
        try:
            sock.sendall(b"POST /api/upload HTTP/1.1\r\n"
                         b"Host: 127.0.0.1\r\n"
                         b"Connection: close\r\n"
                         b"Content-Type: application/octet-stream\r\n"
                         b"X-Filename: a.wav\r\n"
                         b"Content-Length: 100\r\n\r\n"
                         + b"x" * 10)   # 必须用 + 连接：`b"a" b"x" * 10` 会先结合成 b"ax" 再乘
            sock.shutdown(socket.SHUT_WR)
            data = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
        finally:
            sock.close()
        self.assertIn(b"400", data.split(b"\r\n")[0])
        self.assertIn(b"DR-2006", data)

    def test_over_limit(self):
        blob = b"\0" * 64
        with mock.patch.object(app, "MAX_UPLOAD", 10):
            status, _, body = request("POST", "/api/upload", body=blob,
                                      headers={"Content-Length": str(len(blob)),
                                               "X-Filename": "a.mp4"})
        self.assertEqual(status, 413)
        self.assertEqual(json.loads(body)["code"], "DR-2002")

    def test_rejects_bad_extension(self):
        blob = b"\0" * 32
        status, _, body = request("POST", "/api/upload", body=blob,
                                  headers={"Content-Length": str(len(blob)),
                                           "X-Filename": "evil.sh"})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["code"], "DR-2003")

    def test_accepts_video_and_reports(self):
        blob = b"\0" * 4096
        status, _, body = request("POST", "/api/upload", body=blob,
                                  headers={"Content-Length": str(len(blob)),
                                           "X-Filename": "demo.mp4"})
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["bytes"], len(blob))
        self.assertEqual(data["name"], "demo.mp4")
        self.assertTrue(data["id"].isalnum())
        for key in ("duration", "asr", "remaining"):
            self.assertIn(key, data)
        saved = sorted(app.UPLOAD_DIR.glob(f"{data['id']}__*"))
        self.assertTrue(saved, "上传件应落盘")
        self.assertEqual(saved[0].stat().st_size, len(blob))
        saved[0].unlink(missing_ok=True)

    def test_accepts_punctuated_filename(self):
        """带中文标点、扩展名后还有句号的文件名，不该再被判「不支持的类型」。"""
        blob = b"\0" * 32
        for raw in ("顾植山老师说阴平阳秘（上）.mp4", "视频.mp4。", "视频．mp4"):
            status, _, body = request(
                "POST", "/api/upload", body=blob,
                headers={"Content-Length": str(len(blob)),
                         "X-Filename": quote(raw)})
            self.assertEqual(status, 200, f"{raw} → {body!r}")
            name = json.loads(body)["name"]
            self.assertTrue(name.endswith(".mp4"), name)

    def test_upload_rate_limited(self):
        blob = b"\0" * 16
        hdr = {"Content-Length": str(len(blob)), "X-Filename": "demo.mp4"}
        with mock.patch.object(app, "LIMITER", ratelimit.MemoryRateLimiter(1)):
            first, _, _ = request("POST", "/api/upload", body=blob, headers=hdr)
            second, _, body = request("POST", "/api/upload", body=blob, headers=hdr)
        for f in app.UPLOAD_DIR.glob("*__demo.mp4"):
            f.unlink(missing_ok=True)
        self.assertEqual(first, 200)
        self.assertEqual(second, 429)
        self.assertEqual(json.loads(body)["code"], "DR-3001")

    def test_path_traversal_in_filename_sanitized(self):
        blob = b"\0" * 16
        status, _, body = request(
            "POST", "/api/upload", body=blob,
            headers={"Content-Length": str(len(blob)),
                     "X-Filename": "%2E%2E%2F%2E%2E%2Fevil.mp4"})
        self.assertEqual(status, 200)
        name = json.loads(body)["name"]
        self.assertNotIn("/", name)
        self.assertNotIn("..", name)
        for f in app.UPLOAD_DIR.glob("*.mp4"):
            if "evil" in f.name:
                f.unlink(missing_ok=True)


class TestUploadTtl(Base):
    """A：上传件兜底清扫（v2.20）。分析结束会立刻删，这里测「没分析的那些」。"""

    def _touch(self, name, age):
        p = app.UPLOAD_DIR / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
        old = time.time() - age
        os.utime(p, (old, old))
        return p

    def test_expired_file_removed(self):
        p = self._touch("a__old.mp4", app.UPLOAD_TTL + 60)
        self.assertEqual(app._sweep_uploads(), 1)
        self.assertFalse(p.exists())

    def test_fresh_file_kept(self):
        p = self._touch("b__new.mp4", 60)
        self.assertEqual(app._sweep_uploads(), 0)
        self.assertTrue(p.exists())

    def test_missing_dir_returns_zero(self):
        shutil.rmtree(app.UPLOAD_DIR, ignore_errors=True)
        self.assertEqual(app._sweep_uploads(), 0)

    def test_only_expired_removed(self):
        old = self._touch("c__old.mp4", app.UPLOAD_TTL + 60)
        new = self._touch("d__new.mp4", 60)
        self.assertEqual(app._sweep_uploads(), 1)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())

    def test_counts_all_expired(self):
        self._touch("e__1.mp4", app.UPLOAD_TTL + 10)
        self._touch("f__2.mp4", app.UPLOAD_TTL + 20)
        self.assertEqual(app._sweep_uploads(), 2)

    def test_upload_triggers_sweep(self):
        """上传时顺手清扫，不依赖定时任务。"""
        old = self._touch("g__old.mp4", app.UPLOAD_TTL + 60)
        blob = b"\0" * 64
        status, _, _ = request("POST", "/api/upload", body=blob,
                               headers={"Content-Length": str(len(blob)),
                                        "X-Filename": "new.mp4"})
        self.assertEqual(status, 200)
        self.assertFalse(old.exists())


class TestUploadDrop(Base):
    """C：POST /api/upload/drop（v2.20）。"""

    def _upload(self):
        blob = b"\0" * 256
        status, _, body = request("POST", "/api/upload", body=blob,
                                  headers={"Content-Length": str(len(blob)),
                                           "X-Filename": "demo.mp4"})
        self.assertEqual(status, 200)
        return json.loads(body)["id"]

    def test_drop_removes_file(self):
        uid = self._upload()
        self.assertTrue(list(app.UPLOAD_DIR.glob(f"{uid}__*")))
        status, _, body = post_json("/api/upload/drop", {"id": uid})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["removed"], 1)
        self.assertFalse(list(app.UPLOAD_DIR.glob(f"{uid}__*")))

    def test_drop_missing_is_idempotent(self):
        status, _, body = post_json("/api/upload/drop", {"id": "0" * 16})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["removed"], 0)

    def test_drop_bad_id_rejected(self):
        for bad in ("", "xyz", "a" * 15, "a" * 17, "../etc/passwd"):
            status, _, _ = post_json("/api/upload/drop", {"id": bad})
            self.assertEqual(status, 400, bad)

    def test_drop_then_analyze_404(self):
        uid = self._upload()
        post_json("/api/upload/drop", {"id": uid})
        status, _, body = post_json("/api/analyze", {"upload_id": uid})
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["code"], "DR-2005")

    def test_drop_does_not_consume_quota(self):
        """删除是收尾动作，收费会让人宁可留着不删。"""
        uid = self._upload()
        calls = []
        real = app.LIMITER.allow

        def spy(ip):
            calls.append(ip)
            return real(ip)

        with mock.patch.object(app.LIMITER, "allow", side_effect=spy):
            post_json("/api/upload/drop", {"id": uid})
        self.assertEqual(calls, [])


class TestConcurrency(Base):
    """并发额度：满了给 503，不排队；成功与失败都要把额度还回来。"""

    def test_busy_analyze_returns_503(self):
        with mock.patch.object(app, "_slots", threading.Semaphore(0)), \
                mock.patch.object(app, "SLOT_WAIT", 0.05):
            status, _, body = post_json("/api/analyze", {"url": "https://example.com/a"})
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body)["code"], "DR-3002")

    def test_busy_upload_returns_503(self):
        blob = b"\0" * 16
        with mock.patch.object(app, "_slots", threading.Semaphore(0)), \
                mock.patch.object(app, "SLOT_WAIT", 0.05):
            status, _, body = request("POST", "/api/upload", body=blob,
                                      headers={"Content-Length": str(len(blob)),
                                               "X-Filename": "a.mp4"})
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body)["code"], "DR-3002")

    def test_slot_released_after_success(self):
        """额度只有 1：第一个没还回来，第二个就会 503。"""
        done = lambda *a, **k: iter([{"stage": "done"}])   # noqa: E731
        with mock.patch.object(app, "_slots", threading.BoundedSemaphore(1)), \
                mock.patch.object(app, "MAX_CONCURRENT", 1), \
                mock.patch.object(app, "SLOT_WAIT", 0.1), \
                mock.patch.object(app.pipeline, "analyze", done):
            first, _, _ = post_json("/api/analyze", {"url": "https://example.com/a"})
            second, _, _ = post_json("/api/analyze", {"url": "https://example.com/a"})
        self.assertEqual((first, second), (200, 200))

    def test_slot_released_after_pipeline_error(self):
        """异常路径也必须还额度，否则一次失败就永久少一个槽。"""
        def boom(*a, **k):
            raise RuntimeError("x")
            yield  # noqa: unreachable

        done = lambda *a, **k: iter([{"stage": "done"}])   # noqa: E731
        with mock.patch.object(app, "_slots", threading.BoundedSemaphore(1)), \
                mock.patch.object(app, "MAX_CONCURRENT", 1), \
                mock.patch.object(app, "SLOT_WAIT", 0.1):
            with mock.patch.object(app.pipeline, "analyze", boom):
                first, _, _ = post_json("/api/analyze", {"url": "https://example.com/a"})
            with mock.patch.object(app.pipeline, "analyze", done):
                second, _, _ = post_json("/api/analyze", {"url": "https://example.com/a"})
        self.assertEqual((first, second), (200, 200))

    def test_no_double_release(self):
        """BoundedSemaphore 多 release 会抛 ValueError，这里让多余释放显形。

        额度为 1 时连跑三次；若代码在两条路径上都 release，
        第二次就会因超额释放把服务打成 500。
        """
        done = lambda *a, **k: iter([{"stage": "done"}])   # noqa: E731
        with mock.patch.object(app, "_slots", threading.BoundedSemaphore(1)), \
                mock.patch.object(app, "MAX_CONCURRENT", 1), \
                mock.patch.object(app, "SLOT_WAIT", 0.1), \
                mock.patch.object(app.pipeline, "analyze", done):
            codes = [post_json("/api/analyze", {"url": "https://example.com/a"})[0]
                     for _ in range(3)]
        self.assertEqual(codes, [200, 200, 200])


class TestClientIp(Base):
    """反代后取真实 IP：只有 peer 可信才采信 X-Real-IP。

    不测这一层的后果（v2.16 之前）：部署到 nginx 后面，全站被算成同一个 IP，
    第一个访客用满额度，所有人 429；而 nginx 那层日志里 IP 是对的，极难排查。
    """

    def setUp(self):
        super().setUp()
        # 这组只关心「谁被计数」，不关心读解过程：换成秒回的假 pipeline
        patcher = mock.patch.object(app.pipeline, "analyze",
                                    lambda *a, **k: iter([{"stage": "done", "status": "ok"}]))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _call(self, real_ip=None):
        extra = {"X-Real-IP": real_ip} if real_ip else None
        return post_json("/api/analyze", {"url": "https://example.com/a"}, extra)

    def test_header_ignored_when_no_trusted_proxy(self):
        """没配可信代理时，头一律不采信 —— 两个不同 IP 仍算同一个人。"""
        with mock.patch.object(app, "LIMITER", ratelimit.MemoryRateLimiter(1)), \
                mock.patch.object(app, "_TRUST_EXACT", frozenset()), \
                mock.patch.object(app, "_TRUST_NETS", ()):
            first, _, _ = self._call("9.9.9.1")
            second, _, body = self._call("9.9.9.2")
        self.assertEqual(first, 200)
        self.assertEqual(second, 429)
        self.assertEqual(json.loads(body)["code"], "DR-3001")

    def test_header_used_when_peer_trusted(self):
        """peer 在名单里时，不同的 X-Real-IP 各算各的额度。"""
        with mock.patch.object(app, "LIMITER", ratelimit.MemoryRateLimiter(1)), \
                mock.patch.object(app, "_TRUST_EXACT", frozenset({"127.0.0.1"})), \
                mock.patch.object(app, "_TRUST_NETS", ()):
            first, _, _ = self._call("9.9.9.1")
            second, _, _ = self._call("9.9.9.2")
            third, _, body = self._call("9.9.9.1")
        self.assertEqual((first, second), (200, 200))
        self.assertEqual(third, 429)
        self.assertEqual(json.loads(body)["code"], "DR-3001")

    def test_cidr_trust(self):
        """按网段信任（容器重建后 IP 会变，只写单 IP 会悄悄失效）。"""
        with mock.patch.object(app, "LIMITER", ratelimit.MemoryRateLimiter(1)), \
                mock.patch.object(app, "_TRUST_EXACT", frozenset()), \
                mock.patch.object(app, "_TRUST_NETS", (ipaddress.ip_network("127.0.0.0/8"),)):
            first, _, _ = self._call("9.9.9.1")
            second, _, _ = self._call("9.9.9.2")
        self.assertEqual((first, second), (200, 200))

    def test_peer_trusted_but_empty_header_falls_back(self):
        """可信代理但没带头时，退回 peer，不能变成空字符串。"""
        with mock.patch.object(app, "LIMITER", ratelimit.MemoryRateLimiter(1)), \
                mock.patch.object(app, "_TRUST_EXACT", frozenset({"127.0.0.1"})), \
                mock.patch.object(app, "_TRUST_NETS", ()):
            first, _, _ = self._call(None)
            second, _, _ = self._call(None)
        self.assertEqual((first, second), (200, 429))

    def test_limiter_unavailable_returns_503(self):
        """Redis 不可用要回 503「服务暂不可用」，不能伪装成「你用超了」。"""

        class Down:
            limit = 4

            def allow(self, ip):
                return False, 0, "unavailable", None

        with mock.patch.object(app, "LIMITER", Down()):
            status, _, body = self._call()
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body)["code"], "DR-3003")


class TestRateWindow(Base):
    def test_window_expires(self):
        """一小时窗口外的记录不该继续占额度。"""
        fake_now = [1_000_000.0]
        lim = ratelimit.MemoryRateLimiter(1)
        with mock.patch.object(ratelimit, "_now", lambda: fake_now[0]):
            self.assertTrue(lim.allow("1.2.3.4")[0])
            self.assertFalse(lim.allow("1.2.3.4")[0])
            fake_now[0] += 3601          # 越过一小时
            self.assertTrue(lim.allow("1.2.3.4")[0])


if __name__ == "__main__":
    unittest.main()
