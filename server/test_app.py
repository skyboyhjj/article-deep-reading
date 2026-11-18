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


def post_json(path, obj):
    return request("POST", path, body=json.dumps(obj).encode(),
                   headers={"Content-Type": "application/json"})


class Base(unittest.TestCase):
    def setUp(self):
        app._rate.clear()
        # 每个用例一个独立的上传目录：共享系统 temp 时，
        # 上次运行残留的文件会让断言时对时错（踩过一次）。
        tmp = Path(tempfile.mkdtemp(prefix="apptest_"))
        patcher = mock.patch.object(app, "UPLOAD_DIR", tmp)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, tmp, True)

    def tearDown(self):
        app._rate.clear()

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
                    "ffmpeg", "max_upload_mb", "rate_limit_per_hour"):
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
        with mock.patch.object(app, "RATE_LIMIT", 1), \
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
        with mock.patch.object(app, "RATE_LIMIT", 1):
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


class TestRateWindow(Base):
    def test_window_expires(self):
        """一小时窗口外的记录不该继续占额度。"""
        fake_now = [1_000_000.0]
        with mock.patch.object(app.time, "time", lambda: fake_now[0]), \
                mock.patch.object(app, "RATE_LIMIT", 1):
            self.assertTrue(app._allow("1.2.3.4")[0])
            self.assertFalse(app._allow("1.2.3.4")[0])
            fake_now[0] += 3601          # 越过一小时
            self.assertTrue(app._allow("1.2.3.4")[0])


if __name__ == "__main__":
    unittest.main()
