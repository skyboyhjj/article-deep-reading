"""媒体处理：抽音轨、探时长、超长切片。

为什么先抽音轨：视频动辄几十上百 MB。抽成 16kHz 单声道后，
   - mp3 32kbps：5 分钟约 2.4MB（通用后端）
   - wav 16bit：5 分钟约 9.6MB（DeepSeek 只收 WAV/Ogg Opus，且 ≤8MiB/≤120s，
     所以走 DeepSeek 时必须按 110 秒切片，见 asr.audio_spec()）

全部走 ffmpeg 子进程（list 参数，不 shell=True）。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

DEFAULT_MAX_SEC = 1200
AUDIO_BITRATE = "32k"


class MediaError(RuntimeError):
    pass


def has_ffmpeg() -> bool:
    return bool(FFMPEG and FFPROBE)


def probe_duration(path: str | Path) -> float:
    """返回时长（秒）。取不到返回 0，不抛异常。"""
    if not FFPROBE:
        return 0.0
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return float(out.stdout.strip() or 0)
    except (subprocess.SubprocessError, ValueError, OSError):
        return 0.0


def extract_audio(src: str | Path, dst: str | Path, fmt: str = "mp3",
                  timeout: int = 600) -> Path:
    """抽音轨。fmt: "mp3"（通用）或 "wav"（DeepSeek 必须 16-bit PCM）。"""
    if not FFMPEG:
        raise MediaError("服务器没有 ffmpeg。请安装 ffmpeg，或自行转成音频后再上传")
    src, dst = Path(src), Path(dst)
    if fmt not in ("mp3", "wav"):
        raise MediaError(f"不支持的音频格式: {fmt}")
    codec = ["-c:a", "pcm_s16le"] if fmt == "wav" else ["-b:a", AUDIO_BITRATE]
    proc = subprocess.run(
        [FFMPEG, "-y", "-loglevel", "error", "-i", str(src),
         "-vn", "-ac", "1", "-ar", "16000"] + codec + [str(dst)],
        capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0 or not dst.exists() or dst.stat().st_size < 1024:
        raise MediaError(f"音轨提取失败：{(proc.stderr or '').strip()[:200]}")
    return dst


def split_audio(src: str | Path, out_dir: str | Path, seg_sec: int = DEFAULT_MAX_SEC,
                fmt: str = "mp3") -> list[Path]:
    """按时间切片，返回按顺序排好的分片。"""
    if not FFMPEG:
        raise MediaError("服务器没有 ffmpeg，无法切片")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pattern = str(out_dir / f"part-%03d.{fmt}")
    # wav 用 copy 会得到不合规的头，改为重编码（PCM 无损，代价很低）
    codec = ["-c:a", "pcm_s16le"] if fmt == "wav" else ["-c", "copy"]
    proc = subprocess.run(
        [FFMPEG, "-y", "-loglevel", "error", "-i", str(src),
         "-f", "segment", "-segment_time", str(seg_sec)] + codec + [pattern],
        capture_output=True, text=True, timeout=900,
    )
    if proc.returncode != 0:
        raise MediaError(f"切片失败：{(proc.stderr or '').strip()[:200]}")
    parts = sorted(out_dir.glob(f"part-*.{fmt}"))
    if not parts:
        raise MediaError("切片后没有生成文件")
    return parts


def prepare(src: str | Path, workdir: str | Path,
            max_sec: int = DEFAULT_MAX_SEC, fmt: str = "mp3") -> dict:
    """把音视频整理成「可送 ASR 的分片列表」。

    返回 {parts:[Path], duration:float, audio:Path, seg_sec:int, fmt:str}
    """
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    src = Path(src)
    if not src.exists():
        raise MediaError(f"文件不存在：{src}")

    duration = probe_duration(src)
    audio = extract_audio(src, workdir / f"audio.{fmt}", fmt=fmt)

    if duration and duration > max_sec:
        parts = split_audio(audio, workdir / "parts", max_sec, fmt=fmt)
        return {"parts": parts, "duration": duration, "audio": audio,
                "seg_sec": max_sec, "fmt": fmt}
    return {"parts": [audio], "duration": duration, "audio": audio,
            "seg_sec": max_sec, "fmt": fmt}


_SAFE = re.compile(r"[^\w.\-]+")


def safe_filename(name: str, default: str = "upload") -> str:
    base = os.path.basename(name or default)
    base = _SAFE.sub("_", base).strip("._") or default
    return base[:80]
