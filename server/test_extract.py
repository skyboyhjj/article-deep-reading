#!/usr/bin/env python3
"""抓取层的回归用例（全程不联网）。

起因：`extract.py` 用了 `os.environ.get(...)`（fetch / deepseek_fetch 里），
却没有 `import os`。任何走 `fetch()` 的链接都会在 FETCH_BACKEND 判断那一行
抛 `NameError` —— 公众号、arXiv、通用网页三条路一起死。

CI 没抓到，因为 `test_pipeline_e2e.py` 走的是「粘贴正文 + 假模型」，
绕开了真实抓取。所以这里的用例一律**真的调用 `extract.fetch()`**，
只把最底层的 `_get` / `deepseek_fetch` 换成夹具，
保证 `fetch()` 函数体（含环境变量读取）逐行被执行到。

不追求判准真实页面，只保证：
  ① 三档 FETCH_BACKEND 与未设置都不崩；
  ② 标题/正文/公式/发布时间能被抽出来；
  ③ 块级标签没把两段粘成一段（踩过的坑）；
  ④ 本地失败能回退 DeepSeek，且降级写进 notes。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import extract  # noqa: E402

GENERIC_HTML = """<html><head>
<title>落回用的标题</title>
<meta property="og:title" content="通用网页标题">
<style>.x{color:red}</style>
</head><body>
<nav>导航不该出现</nav>
<article>
  <p>第一段正文，讲的是某件事。</p>
  <p>第二段正文，接着讲。</p>
</article>
<script>var junk = 1;</script>
</body></html>"""

WECHAT_HTML = """<html><body>
<h1 class="rich_media_title">公众号标题</h1>
<em id="js_name">某公众号</em>
<script>var ct = "1794563307";</script>
<div id="js_content" class="rich_media_content">
<p>前置段落。</p>
<pre>定制环境 = 静态环境 + EnvHarness</pre>
<p>紧随其后的正文段落。</p>
<span data-formula="E=mc^2">公式图</span>
</div>
<div id="js_tags"></div>
</body></html>"""


def _clean_env(**kw):
    """清掉影响抓取分支的环境变量，再按需注入。"""
    for k in ("FETCH_BACKEND", "DFS_API_KEY", "DFS_BASE_URL"):
        os.environ.pop(k, None)
    for k, v in kw.items():
        os.environ[k] = v


class TestFetchBackendPaths(unittest.TestCase):
    """本 bug 的直接回归：fetch() 任何一档都不许抛 NameError。"""

    def test_all_backends_reach_local_parser(self):
        for backend in (None, "auto", "local", "deepseek"):
            with self.subTest(backend=backend):
                _clean_env(FETCH_BACKEND=backend) if backend else _clean_env()
                with mock.patch.object(extract, "_get", return_value=GENERIC_HTML), \
                        mock.patch.object(extract, "deepseek_fetch", return_value=None):
                    doc = extract.fetch("https://example.com/a")
                self.assertEqual(doc["kind"], "generic")
                self.assertIn("第一段正文", doc["text"])

    def test_env_read_does_not_raise_nameerror(self):
        """把环境变量清空（最容易被忽略的情形）也要能跑通。"""
        _clean_env()
        with mock.patch.object(extract, "_get", return_value=GENERIC_HTML):
            doc = extract.fetch("https://example.com/b")
        self.assertIn("第二段正文", doc["text"])

    def test_deepseek_backend_falls_back_with_note(self):
        _clean_env(FETCH_BACKEND="deepseek")
        with mock.patch.object(extract, "_get", return_value=GENERIC_HTML), \
                mock.patch.object(extract, "deepseek_fetch", return_value=None):
            doc = extract.fetch("https://example.com/c")
        self.assertTrue(any("回退本地" in n for n in doc["notes"]))

    def test_local_failure_falls_back_to_deepseek(self):
        _clean_env()
        long_text = "DeepSeek 取回的正文。" * 60
        with mock.patch.object(extract, "_get", side_effect=extract.FetchError("反爬")), \
                mock.patch.object(extract, "deepseek_fetch",
                                  return_value={"title": "远端标题", "content": long_text}):
            doc = extract.fetch("https://example.com/d")
        self.assertEqual(doc["kind"], "dfs_fetch")
        self.assertEqual(doc["title"], "远端标题")
        self.assertTrue(any("未从页面解析" in n for n in doc["notes"]))


class TestGenericParsing(unittest.TestCase):
    def test_title_and_noise_stripped(self):
        _clean_env()
        with mock.patch.object(extract, "_get", return_value=GENERIC_HTML):
            doc = extract.fetch("https://example.com/e")
        self.assertEqual(doc["title"], "通用网页标题")
        self.assertIn("第一段正文", doc["text"])
        self.assertNotIn("导航不该出现", doc["text"])
        self.assertNotIn("var junk", doc["text"])

    def test_short_body_warns_to_paste(self):
        _clean_env()
        with mock.patch.object(extract, "_get", return_value="<html><body><p>短</p></body></html>"):
            doc = extract.fetch("https://example.com/f")
        self.assertTrue(any("粘贴正文" in n for n in doc["notes"]))


class TestWechatParsing(unittest.TestCase):
    def test_pre_block_not_glued_to_next_paragraph(self):
        """`</pre>` 漏映射为换行会把代码块与下一段粘成一句（历史坑）。"""
        _clean_env()
        with mock.patch.object(extract, "_get", return_value=WECHAT_HTML):
            doc = extract.fetch("https://mp.weixin.qq.com/s/abc")
        self.assertIn("定制环境 = 静态环境 + EnvHarness", doc["text"])
        self.assertNotIn("EnvHarness紧随", doc["text"])
        self.assertNotIn("EnvHarness 紧随", doc["text"])

    def test_formula_restored_from_data_attribute(self):
        _clean_env()
        with mock.patch.object(extract, "_get", return_value=WECHAT_HTML):
            doc = extract.fetch("https://mp.weixin.qq.com/s/abc")
        self.assertIn("$E=mc^2$", doc["text"])

    def test_published_is_beijing_time(self):
        """var ct 是 UTC 秒，直接 fromtimestamp 会差 8 小时。"""
        _clean_env()
        with mock.patch.object(extract, "_get", return_value=WECHAT_HTML):
            doc = extract.fetch("https://mp.weixin.qq.com/s/abc")
        self.assertEqual(doc["published"], "2026-11-13 17:48")
        self.assertEqual(doc["authors"], ["某公众号"])

    def test_missing_container_raises_with_hint(self):
        _clean_env()
        with mock.patch.object(extract, "_get", return_value="<html><body>验证页</body></html>"):
            with self.assertRaises(extract.FetchError) as ctx:
                extract.fetch("https://mp.weixin.qq.com/s/abc")
        self.assertIn("js_content", str(ctx.exception))


class TestBadInput(unittest.TestCase):
    def test_reject_non_http(self):
        _clean_env()
        with self.assertRaises(extract.FetchError):
            extract.fetch("file:///etc/passwd")


if __name__ == "__main__":
    unittest.main()
