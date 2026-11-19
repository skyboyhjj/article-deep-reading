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
from unittest import mock

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


class TestGet(Base):
    def test_index_served(self):
        status, headers, body = request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertGreater(len(body), 500)

    def test_index_html_alias(self):
        status, _, body = request("GET", "/index.html")
        self.assertEqual(status, 200)
        self.assertEqual(body, request("GET", "/")[2])

    def test_health_shape_and_no_secret(self):
        os.environ["LLM_API_KEY"] = "sk-should-never-appear"
        try:
            status, _, body = request("GET", "/api/health")
        finally:
            os.environ.pop("LLM_API_KEY", None)
        self.assertEqual(status, 200)
        data = json.loads(body)
        for key in ("ok", "llm", "asr", "asr_backend", "search", "search_backend",
                    "ffmpeg", "max_upload_mb", "rate_limit_per_hour", "max_concurrent"):
            self.assertIn(key, data)
        self.assertNotIn(b"sk-should-never-appear", body)

    def test_unknown_path_404(self):
        status, _, body = request("GET", "/nope")
        self.assertEqual(status, 404)
        self.assertIn("not found", json.loads(body)["error"])


class TestAnalyzeValidation(Base):
    """这些分支都在进入 pipeline 之前返回，所以不花钱。"""

    def test_empty_body(self):
        status, _, body = request("POST", "/api/analyze", body=b"",
                                  headers={"Content-Length": "0"})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"], "请求体缺失")

    def test_oversize_body(self):
        payload = b"x" * 260_001
        status, _, body = request("POST", "/api/analyze", body=payload,
                                  headers={"Content-Length": str(len(payload))})
        self.assertEqual(status, 400)
        self.assertIn("过大", json.loads(body)["error"])

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
        self.assertIn("JSON 解析失败", json.loads(body)["error"])

    def test_upload_id_not_alnum(self):
        status, _, body = post_json("/api/analyze", {"upload_id": "../../etc"})
        self.assertEqual(status, 400)
        self.assertIn("upload_id 非法", json.loads(body)["error"])

    def test_upload_id_missing_on_disk(self):
        status, _, body = post_json("/api/analyze", {"upload_id": "deadbeef0000"})
        self.assertEqual(status, 404)
        self.assertIn("不存在或已过期", json.loads(body)["error"])

    def test_nothing_provided(self):
        status, _, body = post_json("/api/analyze", {"url": "ftp://x/y"})
        self.assertEqual(status, 400)
        self.assertIn("请提供链接", json.loads(body)["error"])

    def test_pasted_text_too_long(self):
        """用 ASCII 才测得到这条分支：20 万字符 = 20 万字节，未撞 26 万字节的体量上限。"""
        status, _, body = post_json("/api/analyze", {"text": "x" * 200_001})
        self.assertEqual(status, 400)
        self.assertIn("过长", json.loads(body)["error"])

    def test_chinese_hits_body_limit_first(self):
        """中文 20 万字符 = 60 万字节，先撞 260 KB 那道闸。

        两道限制在中文下并不重叠：字符上限（20 万）永远走不到，
        实际可用量由字节闸决定 —— 约 8.6 万汉字。
        所以那条错误文案必须把这个数说出来，否则用户会对着
        「上限 20 万字符」白贴一遍。v2.8 已改。
        """
        status, _, body = post_json("/api/analyze", {"text": "字" * 200_001})
        self.assertEqual(status, 400)
        msg = json.loads(body)["error"]
        self.assertIn("请求体过大", msg)
        self.assertIn("8.6 万汉字", msg)
        self.assertIn("分段", msg)

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

    def test_pipeline_exception_becomes_error_event(self):
        def boom(*a, **k):
            raise RuntimeError("模型挂了")
            yield  # 让它成为生成器
        with mock.patch.object(app.pipeline, "analyze", boom):
            status, _, body = post_json("/api/analyze", {"url": "https://example.com/b"})
        self.assertEqual(status, 200)
        text = body.decode()
        self.assertIn('"stage": "error"', text)
        self.assertIn("服务异常", text)
        self.assertIn("模型挂了", text)

    def test_rate_limit_blocks_second_call(self):
        with mock.patch.object(app, "LIMITER", ratelimit.MemoryRateLimiter(1)), \
                mock.patch.object(app.pipeline, "analyze", self._fake({"stage": "done"})):
            first, _, _ = post_json("/api/analyze", {"url": "https://example.com/c"})
            second, _, body = post_json("/api/analyze", {"url": "https://example.com/c"})
        self.assertEqual(first, 200)
        self.assertEqual(second, 429)
        self.assertIn("每小时限", json.loads(body)["error"])

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


class TestUpload(Base):
    def test_missing_content_length(self):
        status, _, body = request("POST", "/api/upload", body=b"",
                                  headers={"X-Filename": "a.mp4"})
        self.assertEqual(status, 400)
        self.assertIn("缺少 Content-Length", json.loads(body)["error"])

    def test_over_limit(self):
        blob = b"\0" * 64
        with mock.patch.object(app, "MAX_UPLOAD", 10):
            status, _, body = request("POST", "/api/upload", body=blob,
                                      headers={"Content-Length": str(len(blob)),
                                               "X-Filename": "a.mp4"})
        self.assertEqual(status, 413)
        self.assertIn("超过上限", json.loads(body)["error"])

    def test_rejects_bad_extension(self):
        blob = b"\0" * 32
        status, _, body = request("POST", "/api/upload", body=blob,
                                  headers={"Content-Length": str(len(blob)),
                                           "X-Filename": "evil.sh"})
        self.assertEqual(status, 400)
        self.assertIn("不支持的文件类型", json.loads(body)["error"])

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
        self.assertIn("每小时限", json.loads(body)["error"])

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
        self.assertIn("不存在或已过期", json.loads(body)["error"])

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
        self.assertIn("同时进行的任务已达", json.loads(body)["error"])

    def test_busy_upload_returns_503(self):
        blob = b"\0" * 16
        with mock.patch.object(app, "_slots", threading.Semaphore(0)), \
                mock.patch.object(app, "SLOT_WAIT", 0.05):
            status, _, body = request("POST", "/api/upload", body=blob,
                                      headers={"Content-Length": str(len(blob)),
                                               "X-Filename": "a.mp4"})
        self.assertEqual(status, 503)
        self.assertIn("同时进行的任务已达", json.loads(body)["error"])

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
        self.assertIn("每小时限", json.loads(body)["error"])

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
        self.assertIn("每小时限", json.loads(body)["error"])

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
        self.assertIn("限流服务暂不可用", json.loads(body)["error"])


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
