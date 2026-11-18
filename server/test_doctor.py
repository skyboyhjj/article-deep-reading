#!/usr/bin/env python3
"""体检脚本 doctor.py 的用例（不联网、不打印任何 key）。

doctor.py 是「代码没毛病、环境有毛病」时的唯一入口，
它的失败模式很特殊：**它自己崩掉，用户就彻底没了线索**。
所以这里只验三件事：

  ① 代理地址里的账号密码必须被遮蔽 —— 体检输出常被贴进群里；
  ② certifi 缺失、TLS 校验失败、连不上，三种结局各有明确文案，都不抛异常；
  ③ main() 在无网络环境也能跑完并返回 0。
"""
import io
import os
import ssl
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import doctor  # noqa: E402

ENV_KEYS = ("CA_BUNDLE", "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE", "SSL_CERT_DIR",
            "INSECURE_TLS", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
            "http_proxy", "https_proxy", "all_proxy", "NO_PROXY")


def run_capture(fn, *args):
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn(*args)
    return buf.getvalue()


class TestMask(unittest.TestCase):
    def test_masks_credentials(self):
        self.assertEqual(doctor._mask("http://user:pass@127.0.0.1:8080"),
                         "http://***@127.0.0.1:8080")
        self.assertEqual(doctor._mask("http://token@proxy:80"), "http://***@proxy:80")

    def test_plain_url_untouched(self):
        self.assertEqual(doctor._mask("http://127.0.0.1:8080"), "http://127.0.0.1:8080")
        self.assertEqual(doctor._mask(""), "")

    def test_password_never_leaks(self):
        out = doctor._mask("socks5://alice:Secr3t!@10.0.0.1:1080")
        self.assertNotIn("Secr3t", out)
        self.assertNotIn("alice", out)
        self.assertIn("10.0.0.1:1080", out)


class TestCheckEnv(unittest.TestCase):
    def setUp(self):
        for k in ENV_KEYS:
            os.environ.pop(k, None)

    def tearDown(self):
        for k in ENV_KEYS:
            os.environ.pop(k, None)

    def test_empty_says_none(self):
        out = run_capture(doctor.check_env)
        self.assertIn("（无）", out)

    def test_proxy_masked_in_output(self):
        os.environ["HTTPS_PROXY"] = "http://bob:hunter2@127.0.0.1:8888"
        out = run_capture(doctor.check_env)
        self.assertNotIn("hunter2", out)
        self.assertNotIn("bob", out)
        self.assertIn("***@127.0.0.1:8888", out)

    def test_insecure_warning_only_when_on(self):
        out = run_capture(doctor.check_env)
        self.assertNotIn("INSECURE_TLS 已开启", out)
        os.environ["INSECURE_TLS"] = "1"
        self.assertIn("INSECURE_TLS 已开启", run_capture(doctor.check_env))
        os.environ["INSECURE_TLS"] = "0"
        self.assertNotIn("INSECURE_TLS 已开启", run_capture(doctor.check_env))


class TestCheckCertifi(unittest.TestCase):
    def test_missing_certifi_gives_actionable_text(self):
        with mock.patch.dict(sys.modules, {"certifi": None}):
            out = run_capture(doctor.check_certifi)
        self.assertIn("未安装", out)
        self.assertIn("pip install certifi", out)

    def test_present_reports_count(self):
        try:
            import certifi  # noqa: F401
        except ImportError:
            self.skipTest("环境无 certifi")
        out = run_capture(doctor.check_certifi)
        self.assertIn("路径", out)
        self.assertIn("证书数", out)
        self.assertIn("存在    True", out)


class TestCheckTls(unittest.TestCase):
    """三种结局都要有文案，都不许抛出去 —— 体检自己崩了就没了线索。"""

    def test_ok_path_reports_success(self):
        class FakeSS:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def getpeercert(self):
                return {"issuer": ((("organizationName", "Test CA"),),),
                        "subject": ((("commonName", "api.deepseek.com"),),),
                        "notBefore": "Jan  1 00:00:00 2026 GMT",
                        "notAfter": "Jan  1 00:00:00 2027 GMT"}

            def version(self):
                return "TLSv1.3"

        class FakeSock:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        ctx = mock.Mock()
        ctx.wrap_socket.return_value = FakeSS()
        with mock.patch.object(doctor.socket, "create_connection", return_value=FakeSock()), \
                mock.patch.object(doctor.ssl, "create_default_context", return_value=ctx):
            out = run_capture(doctor.check_tls, "api.deepseek.com")
        self.assertIn("结果    成功", out)
        self.assertIn("Test CA", out)

    def test_cert_verification_failure_explained(self):
        exc = ssl.SSLCertVerificationError(1, "certificate verify failed: unable to get local issuer")
        with mock.patch.object(doctor.socket, "create_connection", side_effect=exc):
            out = run_capture(doctor.check_tls, "api.deepseek.com")
        self.assertIn("证书校验失败", out)
        self.assertIn("中间人代理或根证书缺失", out)

    def test_timeout_explained(self):
        with mock.patch.object(doctor.socket, "create_connection",
                               side_effect=TimeoutError("timed out")):
            out = run_capture(doctor.check_tls, "api.deepseek.com")
        self.assertIn("连不上", out)


class TestMain(unittest.TestCase):
    def test_runs_through_without_network(self):
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        with mock.patch.object(doctor, "check_tls", lambda h: None):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = doctor.main()
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        for section in ("读解服务 · 环境体检", "=== Python ===", "=== 根证书（certifi） ===",
                        "=== 相关环境变量 ===", "=== requests 实际使用的配置 ===",
                        "=== 如果上面有失败项 ==="):
            self.assertIn(section, out)

    def test_suggestion_mentions_insecure_is_last_resort(self):
        out = run_capture(doctor.suggestion)
        self.assertIn("INSECURE_TLS=1", out)
        self.assertIn("别在真实环境里用", out)


if __name__ == "__main__":
    unittest.main()
