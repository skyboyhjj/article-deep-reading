#!/usr/bin/env python3
"""端到端检查：输入在「网址 ↔ 上传音视频」之间切换时，报告头是否正确。

不联网、不花钱。本地起三样东西：
  ① 假模型端点（chat/completions + tools/asr + 一个公众号格式的假页面）
  ② 真 app 服务（子进程，环境变量指向假端点）
  ③ 一份现场生成的 8 秒 WAV

每组跑三遍：
  A 只给网址      → 报告应来自文章（有作者、有发布时间）
  B 只给上传件    → 报告应来自视频（标题是文件名，无作者/时间）
  C 两者都给      → 必须显式写「来源构成」，否则读起来像两个来源混在一起

C 组是 v2.13 修的那个 bug 的守门用例：前端若不清 URL 框，用户上传视频时
旧链接会被一起提交，报告头变成「文章标题 + 视频正文」的杂交体。

用法：
    python3 tools/e2e_switch_check.py              # 跑 2 轮
    python3 tools/e2e_switch_check.py --rounds 3
    python3 tools/e2e_switch_check.py -v           # 打印报告头

退出码：0 全通过 / 1 有断言失败 / 2 环境不满足
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

import requests

ROOT = Path(__file__).resolve().parent.parent
SERVER_DIR = ROOT / "server"

# ---------------------------------------------------------------- 假页面与假模型

WX_PAGE = """<html><head><title>同构、同态、同胚、同伦、同调</title>
<meta property="og:title" content="同构、同态、同胚、同伦、同调"></head><body>
<h1 class="rich_media_title">同构、同态、同胚、同伦、同调</h1>
<em id="js_name">假想的公众号</em>
<script>var ct = "1794563307";</script>
<div id="js_content" class="rich_media_content">
<p>数学里有一组词长得像：同构、同态、同胚、同伦、同调。它们都表示某种「一样」，
但保留的结构各不相同。这篇文章按从强到弱的顺序梳理一遍，并给出每种的判别方法。</p>
<p>同构是最强的：存在双射且保持运算，两个对象在代数上视为同一个。同态只要求保持运算，
不要求可逆，于是允许丢失信息。同胚是拓扑层面的同构，要求双向连续。同伦更松，
只要求两个映射能连续变形到彼此。同调则是用一串阿贝尔群去逼近一个空间的结构。</p>
<p>理解这个梯度的意义在于：谈「两个东西一样」之前，先问一句「在哪个范畴里一样」。</p>
</div>
<div id="js_tags"></div>
</body></html>"""

TRANSCRIPT = (
    "这段音频用来占位。阴平是指阴气平顺，阳秘是指阳气固密。"
    "两者不是对立，而是互相守持。临床上见到阳虚的人，不能一味温补，要看阴是否平。"
    "这里再补几句凑足长度，让下游的定性阶段有足够正文可用。"
) * 4

TRIAGE = {
    "genre": "论文科普", "genre_reason": "转述性内容，无出处",
    "author_note": "页面署名与文中声称已核对。",
    "claims": [{"quote": "阴平是指阴气平顺，阳秘是指阳气固密", "why": "术语解释", "kind": "术语"}],
    "who_speaks": "", "transcript_risks": [], "title_claims": [], "summary_check": [],
}
VERIFY = {"items": [{"n": 1, "verdict": "未核", "evidence": "需查原书", "fix": "标注出处"}]}
CALC = {"items": []}
COMMENTARY = "术语解释与通行注本基本一致，但缺少原文出处，读者无从核对。"

CALLS = {"triage": 0, "verify": 0, "compute": 0, "commentary": 0, "asr": 0, "other": 0}


def _pick(messages) -> str:
    user = messages[-1]["content"] if messages else ""
    if "第一步" in user:
        CALLS["triage"] += 1
        return json.dumps(TRIAGE, ensure_ascii=False)
    if "第二步" in user:
        CALLS["verify"] += 1
        return json.dumps(VERIFY, ensure_ascii=False)
    if "第三步" in user:
        CALLS["compute"] += 1
        return json.dumps(CALC, ensure_ascii=False)
    if "第四步" in user:
        CALLS["commentary"] += 1
        return COMMENTARY
    CALLS["other"] += 1
    return "（假模型未覆盖的调用）"


class Fake(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code: int, payload, ctype="application/json; charset=utf-8"):
        body = payload.encode() if isinstance(payload, str) else payload
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if "mp.weixin.qq.com" in self.path or self.path.startswith("/page.html"):
            self._send(200, WX_PAGE, "text/html; charset=utf-8")
        else:
            self._send(404, '{"error":"not found"}')

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        if self.path.endswith("/tools/asr"):
            CALLS["asr"] += 1
            self._send(200, json.dumps({"request_id": "fake", "text": TRANSCRIPT,
                                        "duration_ms": 8000}, ensure_ascii=False))
            return
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {}
        content = _pick(payload.get("messages") or [])
        self._send(200, json.dumps({"choices": [{"message": {"content": content}}]},
                                   ensure_ascii=False))


# ---------------------------------------------------------------- 工具函数

def free_port() -> int:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def make_wav(path: Path, seconds: int = 8, rate: int = 16000) -> None:
    """现场生成一段 440Hz 正弦，不依赖外部命令。"""
    import array
    import math
    frames = array.array("h")
    amp = 8000
    for i in range(rate * seconds):
        frames.append(int(amp * math.sin(2 * math.pi * 440 * i / rate)))
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(frames.tobytes())


def wait_health(url: str, timeout: float = 30.0) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            r = requests.get(url, timeout=3)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        time.sleep(0.3)
    raise RuntimeError(f"服务没起来：{url}")


def upload(app_port: int, audio: Path, name: str = "测试音频.mp4") -> str:
    size = audio.stat().st_size
    with audio.open("rb") as fh:
        r = requests.post(f"http://127.0.0.1:{app_port}/api/upload", data=fh,
                          headers={"X-Filename": quote(name), "Content-Length": str(size)},
                          timeout=60)
    r.raise_for_status()
    return r.json()["id"]


def analyze(app_port: int, payload: dict) -> str:
    md = ""
    with requests.post(f"http://127.0.0.1:{app_port}/api/analyze", json=payload,
                       stream=True, timeout=300) as r:
        r.raise_for_status()
        buf = ""
        for chunk in r.iter_content(chunk_size=None, decode_unicode=True):
            buf += chunk or ""
            while "\n\n" in buf:
                block, buf = buf.split("\n\n", 1)
                for line in block.splitlines():
                    if line.startswith("data:"):
                        try:
                            ev = json.loads(line[5:].strip())
                        except Exception:
                            continue
                        if ev.get("stage") == "done":
                            md = (ev.get("data") or {}).get("md", "")
                        elif ev.get("status") == "fail":
                            raise RuntimeError(f"阶段 {ev.get('stage')} 失败：{ev.get('text')}")
    return md


def header_of(md: str, n: int = 10) -> str:
    return "\n".join([ln for ln in md.splitlines() if ln.strip()][:n])


# ---------------------------------------------------------------- 三组断言

def check_article(md: str) -> list[tuple[str, bool]]:
    head = header_of(md, 6)
    return [
        ("A1 标题取自文章", "同构、同态、同胚" in head),
        ("A2 有作者", "- 作者" in head),
        ("A3 有发布信息", "- 发布信息" in head),
        ("A4 不标来源构成（纯链接）", "来源构成" not in md),
        ("A5 类型显示为中文", "微信公众号" in head and "wechat" not in head),
    ]


def check_video(md: str, name: str) -> list[tuple[str, bool]]:
    head = header_of(md, 8)
    return [
        ("B1 标题取自文件名", name in head),
        ("B2 无作者", "- 作者" not in head),
        ("B3 无发布信息", "- 发布信息" not in head),
        ("B4 类型显示为「上传的音视频」", "上传的音视频" in head and "video_upload" not in head),
        ("B5 标了来源层级 L0", "L0" in head),
    ]


def check_mixed(md: str) -> list[tuple[str, bool]]:
    head = header_of(md, 10)
    return [
        ("C1 有「来源构成」说明", "来源构成" in head),
        ("C2 说明里点明正文来自音视频", "正文来自上传音视频的转录" in md),
        ("C3 元信息仍取自链接（可核对）", "- 作者" in head or "- 发布信息" in head),
        ("C4 内部枚举名不外泄", "video_upload" not in md),
    ]


# ---------------------------------------------------------------- 主流程

def main() -> int:
    ap = argparse.ArgumentParser(description="输入切换端到端检查（不联网、不花钱）")
    ap.add_argument("--rounds", type=int, default=2, help="重复几轮（默认 2）")
    ap.add_argument("-v", "--verbose", action="store_true", help="打印报告头")
    args = ap.parse_args()

    if not shutil_which("ffmpeg"):
        print("需要 ffmpeg：媒体层要用它抽音轨。装上再跑。")
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="e2e_switch_"))
    audio = tmp / "tone.wav"
    make_wav(audio)

    fake_port = free_port()
    fake = ThreadingHTTPServer(("127.0.0.1", fake_port), Fake)
    threading.Thread(target=fake.serve_forever, daemon=True).start()

    app_port = free_port()
    env = dict(os.environ)
    env.update({
        "LLM_API_KEY": "fake-key",
        "LLM_BASE_URL": f"http://127.0.0.1:{fake_port}/v1",
        "LLM_MODEL": "fake-model",
        "ASR_BASE_URL": f"http://127.0.0.1:{fake_port}",
        "ASR_BACKEND": "deepseek",
        "SEARCH_ENABLED": "off",
        "RATE_LIMIT_PER_HOUR": "200",
        "PORT": str(app_port),
        "HOST": "127.0.0.1",
    })
    proc = subprocess.Popen([sys.executable, str(SERVER_DIR / "app.py"),
                             "-p", str(app_port), "--host", "127.0.0.1"],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    bad = 0
    try:
        health = wait_health(f"http://127.0.0.1:{app_port}/api/health")
        print(f"环境就绪：llm={health.get('llm')} asr={health.get('asr')} "
              f"ffmpeg={health.get('ffmpeg')} 端口={app_port}\n")
        wx = f"http://127.0.0.1:{fake_port}/mp.weixin.qq.com/s/PLACEHOLDER"

        for rnd in range(1, args.rounds + 1):
            print(f"================ 第 {rnd} 轮 ================")
            cases = []

            md = analyze(app_port, {"url": wx})
            cases.append(("A 只给网址", check_article(md), md))

            name = "顾植山老师说阴平阳秘.mp4"
            md = analyze(app_port, {"upload_id": upload(app_port, audio, name)})
            cases.append(("B 只给上传件", check_video(md, name), md))

            md = analyze(app_port, {"url": wx, "upload_id": upload(app_port, audio, name)})
            cases.append(("C 两者同时给", check_mixed(md), md))

            for title, checks, md in cases:
                ok_all = all(ok for _, ok in checks)
                print(f"  [{title}] {'通过' if ok_all else '失败'}")
                for name_, ok in checks:
                    print(f"      {'[ok]  ' if ok else '[FAIL]'} {name_}")
                    if not ok:
                        bad += 1
                if args.verbose:
                    print("      --- 报告头 ---")
                    for ln in header_of(md, 10).splitlines():
                        print("      " + ln)
            print()

        if CALLS["triage"] == 0 or CALLS["asr"] < args.rounds:
            print(f"注意：假模型的调用分布异常 {CALLS} ——"
                  "prompt 的分步标记可能改了，检查脚本要跟着更新。")
            bad += 1
    finally:
        proc.terminate()
        try:
            out = proc.communicate(timeout=10)[0]
        except subprocess.TimeoutExpired:
            proc.kill()
            out = ""
        if out.strip() and args.verbose:
            print("--- 服务端输出 ---")
            print("\n".join(out.strip().splitlines()[-10:]))
        fake.shutdown()
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"假模型调用分布：{CALLS}")
    print("---- " + ("全部通过" if bad == 0 else f"{bad} 项失败"))
    return 0 if bad == 0 else 1


def shutil_which(name: str) -> str | None:
    import shutil
    return shutil.which(name)


if __name__ == "__main__":
    sys.exit(main())
