#!/usr/bin/env python3
"""ASR 适配层的分支用例（全程不联网、不需要 ffmpeg）。

`test_asr.py` 测的是主干：「分片合并、L1→L0 升级、报告节」。
本文件补的是适配层自己的分支 —— 这些以前一条都没测过：

  ① 四种后端怎么判定（deepseek / openai / local / 未配置）+ auto 的两条规则；
  ② 三种后端各自的请求形状与错误映射（413 / 429 / 5xx / 非 JSON / 连接异常）；
  ③ 未配置与空转录必须显式报错，不返回空字符串冒充成功；
  ④ openai 的 verbose_json 降级重试；
  ⑤ 分片偏移三种时长来源的回退次序（duration_ms → ffprobe → seg_sec）。

判据只有一条：**不许把「没配」和「配了但没成功」混成同一种样子**。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import asr  # noqa: E402

ENV_KEYS = ("ASR_BACKEND", "ASR_BASE_URL", "ASR_API_KEY", "ASR_MODEL",
            "ASR_LANGUAGE", "ASR_PROMPT", "LLM_API_KEY")


def clean_env(**kw):
    for k in ENV_KEYS:
        os.environ.pop(k, None)
    for k, v in kw.items():
        os.environ[k] = v


class FakeResp:
    def __init__(self, status=200, payload=None, text="", raises_value_error=False):
        self.status_code = status
        self._payload = payload
        self.text = text
        self._bad_json = raises_value_error

    def json(self):
        if self._bad_json:
            raise ValueError("not json")
        return self._payload


class TestBackendSelection(unittest.TestCase):
    def tearDown(self):
        clean_env()

    def test_defaults(self):
        clean_env()
        cfg = asr.config()
        self.assertEqual(cfg["backend"], "auto")
        self.assertEqual(cfg["base_url"], "https://api.deepseek.com")
        self.assertEqual(cfg["language"], "zh")
        self.assertEqual(cfg["api_key"], "")

    def test_api_key_falls_back_to_llm_key(self):
        clean_env(LLM_API_KEY="sk-llm")
        self.assertEqual(asr.config()["api_key"], "sk-llm")

    def test_auto_picks_deepseek_by_domain(self):
        clean_env(ASR_API_KEY="k", ASR_BASE_URL="https://api.deepseek.com/")
        self.assertEqual(asr.resolve_backend(), "deepseek")

    def test_auto_picks_openai_for_other_domain(self):
        clean_env(ASR_API_KEY="k", ASR_BASE_URL="https://api.openai.com/v1")
        self.assertEqual(asr.resolve_backend(), "openai")

    def test_explicit_backend_requires_key(self):
        clean_env(ASR_BACKEND="deepseek")
        self.assertEqual(asr.resolve_backend(), "")
        self.assertFalse(asr.is_configured())

    def test_auto_falls_back_to_local_whisper(self):
        clean_env()
        with mock.patch.object(asr, "_local_whisper", return_value="/usr/bin/whisper"):
            self.assertEqual(asr.resolve_backend(), "local")

    def test_nothing_configured(self):
        clean_env()
        with mock.patch.object(asr, "_local_whisper", return_value=None):
            self.assertEqual(asr.resolve_backend(), "")
            self.assertIn("未配置", asr.describe())

    def test_describe_per_backend(self):
        clean_env(ASR_API_KEY="k")
        with mock.patch.object(asr, "_local_whisper", return_value=None):
            self.assertTrue(asr.describe().startswith("deepseek"))
        clean_env(ASR_API_KEY="k", ASR_BASE_URL="https://api.openai.com/v1")
        self.assertIn("whisper-1", asr.describe())
        clean_env()
        with mock.patch.object(asr, "_local_whisper", return_value="/usr/bin/whisper"):
            self.assertIn("local whisper", asr.describe())


class TestAudioSpec(unittest.TestCase):
    def tearDown(self):
        clean_env()

    def test_deepseek_wants_wav_110s(self):
        clean_env(ASR_API_KEY="k")
        spec = asr.audio_spec()
        self.assertEqual(spec["fmt"], "wav")
        self.assertEqual(spec["max_sec"], 110)

    def test_generic_wants_mp3(self):
        clean_env(ASR_API_KEY="k", ASR_BASE_URL="https://api.openai.com/v1")
        spec = asr.audio_spec()
        self.assertEqual(spec["fmt"], "mp3")
        self.assertEqual(spec["max_sec"], 1200)


class TestApiRoot(unittest.TestCase):
    def test_normalizes_to_v1(self):
        self.assertEqual(asr._api_root("https://api.deepseek.com"), "https://api.deepseek.com/v1")
        self.assertEqual(asr._api_root("https://api.deepseek.com/"), "https://api.deepseek.com/v1")
        self.assertEqual(asr._api_root("https://api.deepseek.com/v1"), "https://api.deepseek.com/v1")
        self.assertEqual(asr._api_root("https://api.deepseek.com/v1/"), "https://api.deepseek.com/v1")


class TestDeepSeek(unittest.TestCase):
    def setUp(self):
        clean_env(ASR_API_KEY="sk-test")
        self.path = Path(tempfile.mkdtemp(prefix="asrb_")) / "a.wav"
        self.path.write_bytes(b"RIFF" + b"\0" * 64)

    def tearDown(self):
        clean_env()

    def test_success_shape_and_no_extra_fields(self):
        """DeepSeek 不收 model/language/prompt —— 请求里不许出现 data。"""
        resp = FakeResp(payload={"request_id": "r1", "text": " 正文 ", "duration_ms": 12000})
        with mock.patch.object(asr.net, "post", return_value=resp) as post:
            out = asr.transcribe_file(self.path)
        kwargs = post.call_args.kwargs
        self.assertNotIn("data", kwargs)
        self.assertIn("/v1/tools/asr", post.call_args.args[0])
        self.assertEqual(kwargs["files"]["file"][1], self.path.read_bytes())
        self.assertEqual(out["text"], "正文")
        self.assertEqual(out["segments"], [])          # 没有时间戳
        self.assertEqual(out["duration_ms"], 12000)
        self.assertEqual(out["provider"], "deepseek")

    def test_error_codes_mapped(self):
        cases = [(413, "8 MiB"), (429, "并发"), (500, "HTTP 500")]
        for status, hint in cases:
            with self.subTest(status=status):
                with mock.patch.object(asr.net, "post", return_value=FakeResp(status, text="boom")):
                    with self.assertRaises(asr.ASRError) as ctx:
                        asr.transcribe_file(self.path)
                self.assertIn(hint, str(ctx.exception))

    def test_non_json_response(self):
        with mock.patch.object(asr.net, "post",
                               return_value=FakeResp(200, raises_value_error=True, text="<html>")):
            with self.assertRaises(asr.ASRError) as ctx:
                asr.transcribe_file(self.path)
        self.assertIn("不是 JSON", str(ctx.exception))

    def test_connection_error_translated(self):
        with mock.patch.object(asr.net, "post", side_effect=requests.ConnectionError("boom")):
            with self.assertRaises(asr.ASRError) as ctx:
                asr.transcribe_file(self.path)
        self.assertIn("转写请求失败", str(ctx.exception))

    def test_empty_text_is_error_not_success(self):
        resp = FakeResp(payload={"text": "   ", "duration_ms": 1000})
        with mock.patch.object(asr.net, "post", return_value=resp):
            with self.assertRaises(asr.ASRError) as ctx:
                asr.transcribe_file(self.path)
        self.assertIn("静音", str(ctx.exception))


class TestOpenAI(unittest.TestCase):
    def setUp(self):
        clean_env(ASR_API_KEY="sk-test", ASR_BASE_URL="https://api.openai.com/v1")
        self.path = Path(tempfile.mkdtemp(prefix="asrb_")) / "a.mp3"
        self.path.write_bytes(b"ID3" + b"\0" * 64)

    def tearDown(self):
        clean_env()

    def test_verbose_json_downgrade_retry(self):
        """端点不认 verbose_json 时要摘掉它重试，而不是直接失败。

        注意：mock 记录的 kwargs 保存的是 data 字典的**引用**，
        重试时 pop('response_format') 会把先前那条记录一起改掉。
        所以这里在假 post 内部当场快照，才看得出两次请求的真实差异。
        """
        ok = FakeResp(payload={"text": "甲", "segments": [{"start": 0, "end": 1.5, "text": "甲"}]})
        snapshots = []

        def fake_post(url, **kw):
            snapshots.append(dict(kw.get("data") or {}))
            return FakeResp(400, text="bad format") if len(snapshots) == 1 else ok

        with mock.patch.object(asr.net, "post", side_effect=fake_post):
            out = asr.transcribe_file(self.path)
        self.assertEqual(len(snapshots), 2)
        self.assertEqual(snapshots[0]["response_format"], "verbose_json")
        self.assertNotIn("response_format", snapshots[1])
        self.assertEqual(out["segments"], [{"start": 0.0, "end": 1.5, "text": "甲"}])

    def test_language_and_prompt_passed(self):
        clean_env(ASR_API_KEY="sk-test", ASR_BASE_URL="https://api.openai.com/v1",
                  ASR_LANGUAGE="zh", ASR_PROMPT="术语：识境不二")
        with mock.patch.object(asr.net, "post",
                               return_value=FakeResp(payload={"text": "x"})) as post:
            asr.transcribe_file(self.path)
        data = post.call_args.kwargs["data"]
        self.assertEqual(data["language"], "zh")
        self.assertEqual(data["prompt"], "术语：识境不二")

    def test_plain_text_response_accepted(self):
        with mock.patch.object(asr.net, "post",
                               return_value=FakeResp(200, raises_value_error=True, text="纯文本转录")):
            out = asr.transcribe_file(self.path)
        self.assertEqual(out["text"], "纯文本转录")
        self.assertEqual(out["segments"], [])


class TestLocal(unittest.TestCase):
    def setUp(self):
        clean_env(ASR_BACKEND="local")
        self.path = Path(tempfile.mkdtemp(prefix="asrb_")) / "a.mp3"
        self.path.write_bytes(b"ID3")

    def tearDown(self):
        clean_env()

    def test_missing_exe_is_not_configured(self):
        with mock.patch.object(asr, "_local_whisper", return_value=None):
            with self.assertRaises(asr.ASRNotConfigured):
                asr.transcribe_file(self.path)

    def test_returncode_nonzero(self):
        fake = mock.Mock(returncode=2, stderr="model not found", stdout="")
        with mock.patch.object(asr, "_local_whisper", return_value="/usr/bin/whisper"), \
                mock.patch.object(asr.subprocess, "run", return_value=fake):
            with self.assertRaises(asr.ASRError) as ctx:
                asr.transcribe_file(self.path)
        self.assertIn("model not found", str(ctx.exception))

    def test_no_json_produced(self):
        fake = mock.Mock(returncode=0, stderr="", stdout="")
        with mock.patch.object(asr, "_local_whisper", return_value="/usr/bin/whisper"), \
                mock.patch.object(asr.subprocess, "run", return_value=fake):
            with self.assertRaises(asr.ASRError) as ctx:
                asr.transcribe_file(self.path)
        self.assertIn("未产出 json", str(ctx.exception))

    def test_success_reads_segments(self):
        def fake_run(cmd, **kw):
            out_dir = Path(cmd[cmd.index("--output_dir") + 1])
            (out_dir / "a.json").write_text(json.dumps({
                "text": "整段文字",
                "segments": [{"start": 0, "end": 2, "text": "整段"}],
            }), encoding="utf-8")
            return mock.Mock(returncode=0, stderr="", stdout="")

        with mock.patch.object(asr, "_local_whisper", return_value="/usr/bin/whisper"), \
                mock.patch.object(asr.subprocess, "run", side_effect=fake_run):
            out = asr.transcribe_file(self.path)
        self.assertEqual(out["text"], "整段文字")
        self.assertEqual(out["segments"][0]["text"], "整段")
        self.assertEqual(out["provider"], "local")


class TestUnconfigured(unittest.TestCase):
    def tearDown(self):
        clean_env()

    def test_no_backend_raises_not_configured(self):
        clean_env()
        with mock.patch.object(asr, "_local_whisper", return_value=None):
            with self.assertRaises(asr.ASRNotConfigured):
                asr.transcribe_file("whatever.mp3")


class TestPartsOffset(unittest.TestCase):
    """偏移优先用后端给的 duration_ms，其次 ffprobe，最后才用固定 seg_sec。"""

    def tearDown(self):
        clean_env()

    def _run(self, results, probe=None):
        orig = asr.transcribe_file
        asr.transcribe_file = lambda p, timeout=900: results[str(p)]
        try:
            with mock.patch.object(asr, "_probe_seconds", side_effect=probe or (lambda p: 0.0)):
                return asr.transcribe_parts([Path("p1"), Path("p2")], seg_sec=600)
        finally:
            asr.transcribe_file = orig

    def test_offset_from_duration_ms(self):
        out = self._run({
            "p1": {"text": "甲", "segments": [{"start": 0, "end": 1, "text": "甲"}],
                   "duration_ms": 30000, "provider": "d"},
            "p2": {"text": "乙", "segments": [{"start": 0, "end": 1, "text": "乙"}],
                   "duration_ms": 20000, "provider": "d"},
        })
        self.assertEqual([s["start"] for s in out["segments"]], [0.0, 30.0])
        self.assertEqual(out["duration"], 50.0)

    def test_offset_falls_back_to_probe(self):
        out = self._run({
            "p1": {"text": "甲", "segments": [{"start": 0, "end": 1, "text": "甲"}], "provider": "d"},
            "p2": {"text": "乙", "segments": [{"start": 0, "end": 1, "text": "乙"}], "provider": "d"},
        }, probe=lambda p: 45.0)
        self.assertEqual([s["start"] for s in out["segments"]], [0.0, 45.0])

    def test_offset_falls_back_to_seg_sec(self):
        out = self._run({
            "p1": {"text": "甲", "segments": [{"start": 0, "end": 1, "text": "甲"}], "provider": "d"},
            "p2": {"text": "乙", "segments": [{"start": 0, "end": 1, "text": "乙"}], "provider": "d"},
        })
        self.assertEqual([s["start"] for s in out["segments"]], [0.0, 600.0])

    def test_empty_part_text_not_joined(self):
        out = self._run({
            "p1": {"text": "甲", "segments": [], "provider": "d"},
            "p2": {"text": "", "segments": [], "duration_ms": 1000, "provider": "d"},
        })
        self.assertEqual(out["text"], "甲")
        self.assertEqual(out["parts"], 2)


class TestStamp(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(asr.stamp(0), "00:00")
        self.assertEqual(asr.stamp(65.9), "01:05")
        self.assertEqual(asr.stamp(-3), "00:00")


if __name__ == "__main__":
    unittest.main()
