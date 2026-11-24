"""ASR 适配层：把音频变成文字。

支持三种后端，换供应商只改环境变量：
  1. deepseek  POST {root}/tools/asr        （DeepSeek 全家桶 ASR，见下）
  2. openai    兼容 POST {base}/audio/transcriptions（OpenAI / 硅基流动 / 本地服务）
  3. local     本机 whisper CLI

**DeepSeek ASR 的硬约束（与 OpenAI 差异很大，踩点都在这）：**
  - 端点 /v1/tools/asr，multipart 的 file 字段，**没有 model、没有 language、没有 prompt**
  - 只收 **16-bit PCM WAV 或 Ogg Opus**，mp3 会 400
  - 单文件 ≤ 8 MiB、**时长 ≤ 120 秒**、上传须 10 分钟内完成
  - 返回 {"request_id","text","duration_ms"}，**没有 segments 时间戳**
  - 语种自动检测（中文无需指定）

所以走 DeepSeek 时必须：抽 16kHz 单声道 16bit WAV、按 110 秒切片（留余量）。

环境变量：
    ASR_BACKEND   auto | deepseek | openai | local   （默认 auto）
    ASR_BASE_URL  默认 https://api.deepseek.com
    ASR_API_KEY
    ASR_MODEL     仅 openai / local 用（DeepSeek 无此参数）
    ASR_LANGUAGE  仅 openai / local 用
    ASR_PROMPT    仅 openai / local 用（DeepSeek 不收 prompt）
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import net
import requests

# DeepSeek：120 秒上限，留 10 秒余量，避免边界 121 秒被 400
DEEPSEEK_MAX_SEC = 110
DEFAULT_MAX_SEC = 1200


class ASRError(RuntimeError):
    """转写失败（可换文件重试）。"""
    code = "DR-4004"


class ASRNotConfigured(ASRError):
    """没配后端 —— 上层据此保持 L1（元宝总结），并如实说明。"""
    code = "DR-4006"


def config() -> dict:
    return {
        "backend": os.environ.get("ASR_BACKEND", "auto").strip().lower(),
        "base_url": os.environ.get("ASR_BASE_URL", "https://api.deepseek.com").rstrip("/"),
        "api_key": os.environ.get("ASR_API_KEY", "") or os.environ.get("LLM_API_KEY", ""),
        "model": os.environ.get("ASR_MODEL", ""),
        "language": os.environ.get("ASR_LANGUAGE", "zh"),
        "prompt": os.environ.get("ASR_PROMPT", ""),
    }


def _local_whisper() -> str | None:
    return shutil.which("whisper")


def _is_deepseek(cfg: dict) -> bool:
    return "deepseek.com" in cfg["base_url"]


def resolve_backend() -> str:
    """返回实际生效的后端：deepseek / openai / local / ''（未配置）。"""
    cfg = config()
    name = cfg["backend"]
    if name == "deepseek":
        return "deepseek" if cfg["api_key"] else ""
    if name == "openai":
        return "openai" if cfg["api_key"] else ""
    if name == "local":
        return "local" if _local_whisper() else ""
    # auto：地址指向 DeepSeek 就优先 DeepSeek，否则按 key → 本地命令
    if cfg["api_key"]:
        return "deepseek" if _is_deepseek(cfg) else "openai"
    return "local" if _local_whisper() else ""


def is_configured() -> bool:
    return bool(resolve_backend())


def describe() -> str:
    b = resolve_backend()
    if not b:
        return "未配置（需 ASR_API_KEY / LLM_API_KEY，或安装本地 whisper）"
    cfg = config()
    if b == "deepseek":
        return f"deepseek /tools/asr · ≤{DEEPSEEK_MAX_SEC}s 一片 · WAV"
    if b == "openai":
        return f"openai 兼容 · {cfg['base_url']} · {cfg['model'] or 'whisper-1'}"
    return "local whisper CLI"


def audio_spec() -> dict:
    """告诉抓取/媒体层该准备什么格式、切多长。"""
    if resolve_backend() == "deepseek":
        return {"fmt": "wav", "max_sec": DEEPSEEK_MAX_SEC,
                "why": "DeepSeek ASR 只收 WAV/Ogg Opus，且单文件 ≤120s"}
    return {"fmt": "mp3", "max_sec": DEFAULT_MAX_SEC, "why": "通用后端"}


def _api_root(base: str) -> str:
    """把 https://api.deepseek.com 或 .../v1 统一成 .../v1。"""
    b = base.rstrip("/")
    return b if b.endswith("/v1") else b + "/v1"


def _transcribe_deepseek(path: Path, timeout: int) -> dict:
    cfg = config()
    if not cfg["api_key"]:
        raise ASRNotConfigured("ASR_API_KEY / LLM_API_KEY 未设置")
    url = _api_root(cfg["base_url"]) + "/tools/asr"
    blob = path.read_bytes()     # 读成 bytes：重试时不会用到已读空的文件句柄
    try:
        r = net.post(
            url,
            headers={"Authorization": f"Bearer {cfg['api_key']}"},
            files={"file": (path.name, blob, "audio/wav")},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise ASRError(f"转写请求失败：{net.explain(exc)}") from exc

    if r.status_code == 413:
        raise ASRError("文件超过 8 MiB（请缩短切片）")
    if r.status_code == 429:
        raise ASRError("并发过多（账号上限 50 路），稍后重试")
    if r.status_code >= 400:
        raise ASRError(f"DeepSeek ASR HTTP {r.status_code}: {r.text[:200]}")

    try:
        payload = r.json()
    except ValueError as exc:
        raise ASRError(f"响应不是 JSON: {r.text[:200]}") from exc

    text = str(payload.get("text", "")).strip()
    return {
        "text": text,
        "segments": [],                       # DeepSeek 不返回时间戳
        "duration_ms": int(payload.get("duration_ms") or 0),
        "request_id": payload.get("request_id", ""),
    }


def _transcribe_openai(path: Path, language: str, prompt: str, timeout: int) -> dict:
    cfg = config()
    if not cfg["api_key"]:
        raise ASRNotConfigured("ASR_API_KEY 未设置")
    data = {"model": cfg["model"] or "whisper-1", "response_format": "verbose_json"}
    if language:
        data["language"] = language
    if prompt:
        data["prompt"] = prompt

    blob = path.read_bytes()     # 同上：bytes 才可重复发送
    files = {"file": (path.name, blob, "audio/mpeg")}
    try:
        r = net.post(
            f"{cfg['base_url']}/audio/transcriptions",
            headers={"Authorization": f"Bearer {cfg['api_key']}"},
            data=data, files=files, timeout=timeout,
        )
        if r.status_code >= 400 and data.get("response_format") == "verbose_json":
            data.pop("response_format", None)
            r = net.post(
                f"{cfg['base_url']}/audio/transcriptions",
                headers={"Authorization": f"Bearer {cfg['api_key']}"},
                data=data, files={"file": (path.name, blob, "audio/mpeg")},
                timeout=timeout,
            )
    except requests.RequestException as exc:
        raise ASRError(f"转录请求失败：{net.explain(exc)}") from exc

    if r.status_code >= 400:
        raise ASRError(f"ASR HTTP {r.status_code}: {r.text[:200]}")

    try:
        payload = r.json()
    except ValueError:
        return {"text": r.text.strip(), "segments": []}

    if isinstance(payload, dict):
        segs = [
            {"start": float(s.get("start", 0) or 0),
             "end": float(s.get("end", 0) or 0),
             "text": str(s.get("text", "")).strip()}
            for s in payload.get("segments") or []
        ]
        return {"text": str(payload.get("text", "")).strip(), "segments": segs}
    return {"text": str(payload).strip(), "segments": []}


def _transcribe_local(path: Path, language: str, prompt: str, timeout: int) -> dict:
    exe = _local_whisper()
    if not exe:
        raise ASRNotConfigured("本地 whisper 命令不存在")
    cfg = config()
    with tempfile.TemporaryDirectory() as td:
        cmd = [exe, str(path), "--output_format", "json", "--output_dir", td]
        if cfg["model"]:
            cmd += ["--model", cfg["model"]]
        if language:
            cmd += ["--language", language]
        if prompt:
            cmd += ["--initial_prompt", prompt]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            raise ASRError(f"本地 whisper 失败: {(proc.stderr or '').strip()[:200]}")
        outs = list(Path(td).glob("*.json"))
        if not outs:
            raise ASRError("本地 whisper 未产出 json")
        payload = json.loads(outs[0].read_text(encoding="utf-8"))
    segs = [
        {"start": float(s.get("start", 0)), "end": float(s.get("end", 0)),
         "text": str(s.get("text", "")).strip()}
        for s in payload.get("segments") or []
    ]
    return {"text": str(payload.get("text", "")).strip(), "segments": segs}


def transcribe_file(path: str | Path, timeout: int = 900) -> dict:
    """转录单个文件。返回 {text, segments, provider, duration_ms?}。"""
    backend = resolve_backend()
    if not backend:
        raise ASRNotConfigured("ASR 未配置（ASR_API_KEY / LLM_API_KEY，或本地 whisper）")
    cfg = config()
    p = Path(path)
    if backend == "deepseek":
        out = _transcribe_deepseek(p, timeout)
    elif backend == "openai":
        out = _transcribe_openai(p, cfg["language"], cfg["prompt"], timeout)
    else:
        out = _transcribe_local(p, cfg["language"], cfg["prompt"], timeout)
    if not out["text"].strip():
        raise ASRError("转录结果为空（音频可能是静音或格式不被支持）")
    out["provider"] = backend
    return out


def _probe_seconds(path: Path) -> float:
    if not shutil.which("ffprobe"):
        return 0.0
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return float(r.stdout.strip() or 0)
    except (subprocess.SubprocessError, ValueError, OSError):
        return 0.0


def transcribe_parts(parts: list[Path], seg_sec: int = DEFAULT_MAX_SEC) -> dict:
    """按顺序转录多片并合并。

    时间偏移优先用每片的真实时长（ffprobe）或后端返回的 duration_ms，
    都比「固定 seg_sec」准 —— 最后一片通常短，用固定值会让后续偏移全错。
    """
    texts: list[str] = []
    segments: list[dict] = []
    offset = 0.0
    provider = ""
    actual = 0.0
    for p in parts:
        r = transcribe_file(p)
        provider = provider or r.get("provider", "")
        for s in r.get("segments") or []:
            segments.append({"start": s["start"] + offset,
                             "end": s["end"] + offset,
                             "text": s["text"]})
        texts.append(r["text"].strip())
        dur = (r.get("duration_ms") or 0) / 1000.0 or _probe_seconds(p) or seg_sec
        actual += dur
        offset += dur
    return {
        "text": "\n".join(t for t in texts if t),
        "segments": segments,
        "provider": provider,
        "parts": len(parts),
        "duration": round(actual, 1),
    }


def stamp(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"
