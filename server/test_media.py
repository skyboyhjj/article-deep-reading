#!/usr/bin/env python3
"""媒体层的用例：ffmpeg 缺失、切片失败、时长探不到这几条路，此前都没被走过。

`test_asr.py` 只测「有 ffmpeg 且一切顺利」那一支 —— 缺 ffmpeg 时它打一句 skip
就过去了，等于没测。而真实部署里最常见的恰恰是**没装 ffmpeg**：
用户上传视频，服务要么给出可操作的提示，要么抛一个看不懂的 OSError。

所以这里分两层：
  ① 把 `media.FFMPEG` / `media.FFPROBE` 换成假值，用假的 `subprocess.run`
     覆盖全部失败分支与命令形状（不依赖本机装没装 ffmpeg）；
  ② 若本机真有 ffmpeg，再跑一遍真实抽取与切片，验证 wav 分支的产出可读。
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import media  # noqa: E402

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def ok(stdout="", stderr=""):
    return mock.Mock(returncode=0, stdout=stdout, stderr=stderr)


def bad(code=1, stderr="boom"):
    return mock.Mock(returncode=code, stdout="", stderr=stderr)


class TestSafeFilename(unittest.TestCase):
    def test_strips_directory(self):
        self.assertEqual(media.safe_filename("../../etc/passwd"), "passwd")
        self.assertEqual(media.safe_filename("/abs/path/a.mp4"), "a.mp4")

    def test_illegal_chars_replaced(self):
        self.assertEqual(media.safe_filename("a b*c?.mp4"), "a_b_c_.mp4")

    def test_keeps_cjk(self):
        self.assertEqual(media.safe_filename("视频 素材.mp4"), "视频_素材.mp4")

    def test_trims_leading_dots_and_underscores(self):
        self.assertEqual(media.safe_filename("...hidden.mp4"), "hidden.mp4")

    def test_empty_falls_back(self):
        self.assertEqual(media.safe_filename(""), "upload")
        self.assertEqual(media.safe_filename(None), "upload")
        self.assertEqual(media.safe_filename("..."), "upload")

    def test_length_capped(self):
        self.assertEqual(len(media.safe_filename("a" * 200 + ".mp4")), 80)

    def test_windows_backslash_is_not_a_separator(self):
        """Linux 上 basename 不认反斜杠，形状会变成 C:_x_a.mp4 —— 不致命但要知道。"""
        got = media.safe_filename(r"C:\users\x\a.mp4")
        self.assertNotIn("/", got)
        self.assertNotIn("\\", got)


class TestHasFfmpeg(unittest.TestCase):
    def test_both_required(self):
        with mock.patch.object(media, "FFMPEG", "/usr/bin/ffmpeg"), \
                mock.patch.object(media, "FFPROBE", "/usr/bin/ffprobe"):
            self.assertTrue(media.has_ffmpeg())
        with mock.patch.object(media, "FFMPEG", "/usr/bin/ffmpeg"), \
                mock.patch.object(media, "FFPROBE", None):
            self.assertFalse(media.has_ffmpeg())
        with mock.patch.object(media, "FFMPEG", None), \
                mock.patch.object(media, "FFPROBE", None):
            self.assertFalse(media.has_ffmpeg())


class TestProbeDuration(unittest.TestCase):
    def test_without_ffprobe_returns_zero(self):
        with mock.patch.object(media, "FFPROBE", None):
            self.assertEqual(media.probe_duration("a.mp4"), 0.0)

    def test_normal_output(self):
        with mock.patch.object(media, "FFPROBE", "ffprobe"), \
                mock.patch.object(media.subprocess, "run", return_value=ok("12.34\n")):
            self.assertAlmostEqual(media.probe_duration("a.mp4"), 12.34)

    def test_empty_output(self):
        with mock.patch.object(media, "FFPROBE", "ffprobe"), \
                mock.patch.object(media.subprocess, "run", return_value=ok("")):
            self.assertEqual(media.probe_duration("a.mp4"), 0.0)

    def test_garbage_output_is_not_fatal(self):
        with mock.patch.object(media, "FFPROBE", "ffprobe"), \
                mock.patch.object(media.subprocess, "run", return_value=ok("N/A")):
            self.assertEqual(media.probe_duration("a.mp4"), 0.0)

    def test_timeout_is_not_fatal(self):
        with mock.patch.object(media, "FFPROBE", "ffprobe"), \
                mock.patch.object(media.subprocess, "run",
                                  side_effect=subprocess.TimeoutExpired("ffprobe", 30)):
            self.assertEqual(media.probe_duration("a.mp4"), 0.0)

    def test_oserror_is_not_fatal(self):
        with mock.patch.object(media, "FFPROBE", "ffprobe"), \
                mock.patch.object(media.subprocess, "run", side_effect=OSError("exec denied")):
            self.assertEqual(media.probe_duration("a.mp4"), 0.0)


class TestExtractAudio(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="media_"))
        self.src = self.tmp / "v.mp4"
        self.src.write_bytes(b"\0" * 8)
        self.dst = self.tmp / "audio.mp3"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_ffmpeg_gives_actionable_error(self):
        with mock.patch.object(media, "FFMPEG", None):
            with self.assertRaises(media.MediaError) as ctx:
                media.extract_audio(self.src, self.dst)
        self.assertIn("没有 ffmpeg", str(ctx.exception))
        self.assertIn("安装 ffmpeg", str(ctx.exception))

    def test_unsupported_format(self):
        with mock.patch.object(media, "FFMPEG", "ffmpeg"):
            with self.assertRaises(media.MediaError) as ctx:
                media.extract_audio(self.src, self.dst, fmt="flac")
        self.assertIn("不支持的音频格式", str(ctx.exception))

    def _capture(self, fmt):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            dst = Path(cmd[-1])
            dst.write_bytes(b"\0" * 4096)
            return ok()

        with mock.patch.object(media, "FFMPEG", "ffmpeg"), \
                mock.patch.object(media.subprocess, "run", side_effect=fake_run):
            media.extract_audio(self.src, self.dst, fmt=fmt)
        return calls[0]

    def test_mp3_command_shape(self):
        cmd = self._capture("mp3")
        self.assertIn("-ac", cmd)
        self.assertIn("1", cmd)
        self.assertIn("16000", cmd)
        self.assertIn("-vn", cmd)                  # 丢掉视频流
        self.assertIn("-b:a", cmd)
        self.assertNotIn("pcm_s16le", cmd)

    def test_wav_uses_pcm16(self):
        cmd = self._capture("wav")
        self.assertIn("pcm_s16le", cmd)            # DeepSeek 只收 16-bit PCM

    def test_failure_reports_stderr(self):
        with mock.patch.object(media, "FFMPEG", "ffmpeg"), \
                mock.patch.object(media.subprocess, "run", return_value=bad(stderr="Invalid data found")):
            with self.assertRaises(media.MediaError) as ctx:
                media.extract_audio(self.src, self.dst)
        self.assertIn("Invalid data found", str(ctx.exception))

    def test_tiny_output_rejected(self):
        """产出 < 1024 字节视为失败：静音或空片的典型症状。"""
        def fake_run(cmd, **kw):
            Path(cmd[-1]).write_bytes(b"\0" * 10)
            return ok()
        with mock.patch.object(media, "FFMPEG", "ffmpeg"), \
                mock.patch.object(media.subprocess, "run", side_effect=fake_run):
            with self.assertRaises(media.MediaError):
                media.extract_audio(self.src, self.dst)


class TestSplitAudio(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="media_"))
        self.src = self.tmp / "audio.mp3"
        self.src.write_bytes(b"\0" * 4096)
        self.out = self.tmp / "parts"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_ffmpeg(self):
        with mock.patch.object(media, "FFMPEG", None):
            with self.assertRaises(media.MediaError) as ctx:
                media.split_audio(self.src, self.out)
        self.assertIn("无法切片", str(ctx.exception))

    def test_failure_reports_stderr(self):
        with mock.patch.object(media, "FFMPEG", "ffmpeg"), \
                mock.patch.object(media.subprocess, "run", return_value=bad(stderr="segment failed")):
            with self.assertRaises(media.MediaError) as ctx:
                media.split_audio(self.src, self.out)
        self.assertIn("切片失败", str(ctx.exception))

    def test_no_parts_produced(self):
        with mock.patch.object(media, "FFMPEG", "ffmpeg"), \
                mock.patch.object(media.subprocess, "run", return_value=ok()):
            with self.assertRaises(media.MediaError) as ctx:
                media.split_audio(self.src, self.out)
        self.assertIn("没有生成文件", str(ctx.exception))

    def _capture(self, fmt):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            pattern = Path(cmd[-1])
            for i in range(3):
                (pattern.parent / f"part-{i:03d}.{fmt}").write_bytes(b"\0" * 2048)
            return ok()

        with mock.patch.object(media, "FFMPEG", "ffmpeg"), \
                mock.patch.object(media.subprocess, "run", side_effect=fake_run):
            parts = media.split_audio(self.src, self.out, seg_sec=110, fmt=fmt)
        return calls[0], parts

    def test_returns_sorted_parts(self):
        _, parts = self._capture("mp3")
        self.assertEqual([p.name for p in parts],
                         ["part-000.mp3", "part-001.mp3", "part-002.mp3"])

    def test_segment_time_passed(self):
        cmd, _ = self._capture("mp3")
        self.assertIn("-segment_time", cmd)
        self.assertIn("110", cmd)
        self.assertIn("-f", cmd)
        self.assertIn("segment", cmd)

    def test_mp3_uses_stream_copy(self):
        cmd, _ = self._capture("mp3")
        self.assertIn("copy", cmd)                 # 不重编码，快

    def test_wav_reencodes_instead_of_copy(self):
        """wav 用 copy 会得到不合规的头，必须重编码。"""
        cmd, _ = self._capture("wav")
        self.assertIn("pcm_s16le", cmd)
        self.assertNotIn("copy", cmd)


class TestPrepare(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="media_"))
        self.src = self.tmp / "v.mp4"
        self.src.write_bytes(b"\0" * 4096)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_file(self):
        with self.assertRaises(media.MediaError) as ctx:
            media.prepare(self.tmp / "nope.mp4", self.tmp / "w")
        self.assertIn("文件不存在", str(ctx.exception))

    def _prepare_with(self, duration):
        audio = self.tmp / "work" / "audio.mp3"
        with mock.patch.object(media, "probe_duration", lambda p: duration), \
                mock.patch.object(media, "extract_audio",
                                  lambda src, dst, fmt="mp3", timeout=600: (
                                      Path(dst).parent.mkdir(parents=True, exist_ok=True),
                                      Path(dst).write_bytes(b"\0" * 4096),
                                      Path(dst))[-1]), \
                mock.patch.object(media, "split_audio",
                                  side_effect=lambda a, d, s, fmt="mp3": [
                                      Path(d) / "part-000.mp3", Path(d) / "part-001.mp3"]):
            return media.prepare(self.src, self.tmp / "work")

    def test_short_audio_is_single_part(self):
        out = self._prepare_with(300)
        self.assertEqual(len(out["parts"]), 1)
        self.assertEqual(out["duration"], 300)
        self.assertEqual(out["seg_sec"], media.DEFAULT_MAX_SEC)

    def test_long_audio_is_split(self):
        out = self._prepare_with(3000)
        self.assertEqual(len(out["parts"]), 2)
        self.assertEqual(out["seg_sec"], media.DEFAULT_MAX_SEC)

    def test_unknown_duration_is_not_split(self):
        """时长探不到（0）时不能按 0 切片 —— 只能整片送去，让 ASR 自己报错。"""
        out = self._prepare_with(0.0)
        self.assertEqual(len(out["parts"]), 1)
        self.assertEqual(out["duration"], 0.0)

    def test_custom_max_sec(self):
        audio = self.tmp / "work" / "audio.wav"
        with mock.patch.object(media, "probe_duration", lambda p: 200), \
                mock.patch.object(media, "extract_audio",
                                  lambda src, dst, fmt="mp3", timeout=600: (
                                      Path(dst).write_bytes(b"\0" * 4096), Path(dst))[-1]), \
                mock.patch.object(media, "split_audio",
                                  side_effect=lambda a, d, s, fmt="mp3": [Path(d) / "p0.wav"]):
            out = media.prepare(self.src, self.tmp / "work", max_sec=110, fmt="wav")
        self.assertEqual(len(out["parts"]), 1)      # 200 > 110，应切
        self.assertEqual(out["seg_sec"], 110)
        self.assertEqual(out["fmt"], "wav")


@unittest.skipUnless(HAS_FFMPEG, "本机无 ffmpeg，跳过真实抽取")
class TestRealFfmpeg(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="media_real_"))
        self.src = self.tmp / "tone.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
             "-i", "sine=frequency=440:duration=3", "-ac", "1", "-ar", "16000",
             str(self.src)], check=True, timeout=60,
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_extract_wav_and_probe(self):
        dst = self.tmp / "audio.wav"
        media.extract_audio(self.src, dst, fmt="wav")
        self.assertTrue(dst.exists())
        self.assertGreater(dst.stat().st_size, 1024)
        self.assertAlmostEqual(media.probe_duration(dst), 3.0, delta=0.5)

    def test_split_produces_parts(self):
        parts = media.split_audio(self.src, self.tmp / "parts", seg_sec=1, fmt="wav")
        self.assertGreaterEqual(len(parts), 3)


if __name__ == "__main__":
    unittest.main()
