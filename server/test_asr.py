"""ASR 链路夹具：分片时间戳、L1→L0 升级、总结对照节。

不测真实 ASR 服务（那要 key 与网络），只测「接进来之后逻辑对不对」：
  1. ffmpeg 抽音轨与切片能跑（无 ffmpeg 则跳过，不算失败）；
  2. 多分片转录后，时间戳按片长偏移累加；
  3. 转写成功后 doc 的 layer 升到 L0、旧的 L1 说明被清掉；
  4. 报告出现「元宝总结 vs 原声」一节。

运行：python3 test_asr.py
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import asr
import media
import pipeline

TRIAGE = {
    "genre": "视频口播",
    "genre_reason": "有原声转录，无出处",
    "author_note": "账号为搬运方。",
    "who_speaks": "账号转述，讲述者为文案中提到的老师。",
    "claims": [{"quote": "七损八益", "why": "经典引用", "kind": "引用"}],
    "transcript_risks": [],
    "title_claims": [],
    "summary_check": [
        {"point": "内经讲七胜八益", "in_transcript": "无",
         "verdict": "歪曲", "evidence": "转录作「七损八益」"},
        {"point": "养阳是维持生机", "in_transcript": "有",
         "verdict": "忠实", "evidence": "让它更好地生长"},
    ],
}
VERIFY = {"items": [{"n": 1, "verdict": "未核", "evidence": "需查素问原文", "fix": "标注出处"}]}
CALC = {"items": []}


def make_tone(path: Path, seconds: int = 3) -> bool:
    if not media.has_ffmpeg():
        return False
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", f"sine=frequency=440:duration={seconds}", "-ac", "1", "-ar", "16000", str(path)],
        check=True, timeout=60,
    )
    return True


def main() -> int:
    bad = 0
    tmp = Path(tempfile.mkdtemp(prefix="asrtest_"))

    # --- 1. 媒体处理 ---
    tone = tmp / "tone.mp3"
    if make_tone(tone):
        prep = media.prepare(tone, tmp / "w1")
        if prep["audio"].exists() and prep["audio"].stat().st_size > 1024:
            print("[ok] 抽音轨")
        else:
            print("[FAIL] 抽音轨")
            bad += 1
        if 2.5 <= prep["duration"] <= 4.0:
            print(f"[ok] 时长探测 {prep['duration']:.2f}s")
        else:
            print(f"[FAIL] 时长探测异常 {prep['duration']}")
            bad += 1
        prep2 = media.prepare(tone, tmp / "w2", max_sec=1)
        n = len(prep2["parts"])
        print(f"[ok] 强制切片 {n} 片" if n >= 3 else f"[FAIL] 切片数 {n}")
        if n < 3:
            bad += 1
    else:
        print("[skip] 无 ffmpeg，跳过媒体用例")

    # --- 2. 分片时间戳合并 ---
    orig = asr.transcribe_file

    def fake_transcribe(path, timeout=900):
        return {"text": f"片{Path(path).stem}", "segments": [
            {"start": 1.0, "end": 2.0, "text": "甲"}, {"start": 3.0, "end": 4.0, "text": "乙"}],
            "provider": "stub"}

    asr.transcribe_file = fake_transcribe
    try:
        merged = asr.transcribe_parts([Path("p1.mp3"), Path("p2.mp3")], seg_sec=600)
    finally:
        asr.transcribe_file = orig

    starts = [s["start"] for s in merged["segments"]]
    if starts == [1.0, 3.0, 601.0, 603.0]:
        print("[ok] 分片时间戳偏移累加")
    else:
        print(f"[FAIL] 时间戳合并错误 {starts}")
        bad += 1

    # --- 3. L1 → L0 升级 ---
    doc = {
        "url": "https://yb.tencent.com/wx/ct/f/X", "kind": "wechat_channel",
        "layer": "L1 二手：元宝对视频的总结（非视频原话）",
        "title": "夹具", "authors": ["某账号"], "published": "2026年11月16日",
        "text": "元宝总结…", "summary_text": "元宝写的总结正文", "n_chars": 10,
        "notes": ["视频号：正文来自元宝对该视频的总结，不是视频原话（L1 二手）"],
    }
    out = {"text": "这是原声转录的正文。", "segments": [{"start": 0, "end": 1, "text": "x"}],
           "provider": "stub", "parts": 1}
    d = pipeline._promote_to_l0(doc, out, {"duration": 321}, "v.mp4")

    checks = [
        ("层级升为 L0", d["layer"].startswith("L0")),
        ("旧 L1 说明已清", all("L1 二手" not in n for n in d["notes"])),
        ("转录与总结都进正文", "原声转录" in d["text"] and "元宝写的总结正文" in d["text"]),
        ("保留 summary_text", d["summary_text"] == "元宝写的总结正文"),
        ("记下 asr 元数据", d.get("asr", {}).get("provider") == "stub"),
    ]
    for name, ok in checks:
        print(("[ok] " if ok else "[FAIL] ") + name)
        if not ok:
            bad += 1

    # --- 4. 报告里的总结对照节 ---
    md = pipeline.render_report("u", d, TRIAGE, VERIFY, CALC, "评述无数字。")
    rchecks = [
        ("总结对照成节", "元宝总结 vs 原声" in md),
        ("列出歪曲判定", "歪曲" in md and "七损八益" in md),
        ("免责改为原声提示", "本次已使用原声转录" in md),
        ("不再声称需要原声", "需拿到原声转录" not in md),
    ]
    for name, ok in rchecks:
        print(("[ok] " if ok else "[FAIL] ") + name)
        if not ok:
            bad += 1

    # --- 5. 转录后处理 ---
    import os
    os.environ["ASR_FIX"] = "几10万=几十万;及紫外=极紫外"
    txt, applied = pipeline._apply_fix_rules("这里有几10万行代码，涉及及紫外光源")
    checks_fix = [
        ("规则替换生效", "几十万行代码" in txt and "极紫外光源" in txt),
        ("记录修正条目", len(applied) == 2),
        ("未命中的不误改", "其他内容保持原样" not in txt),
    ]
    for name, ok in checks_fix:
        print(("[ok] " if ok else "[FAIL] ") + name)
        if not ok:
            bad += 1
    del os.environ["ASR_FIX"]

    # 校对：模型失败时原样返回，不许丢转录
    orig_json = pipeline.llm.chat_json
    def boom(messages, **kw):
        raise pipeline.llm.LLMError("stub failure")
    pipeline.llm.chat_json = boom
    try:
        same, nofix = pipeline._proofread("原始转录文本")
    finally:
        pipeline.llm.chat_json = orig_json
    print(("[ok] " if (same == "原始转录文本" and nofix == []) else "[FAIL] ")
          + "校对失败时原样返回")

    # 校对：输出长度急剧缩水说明在改写，不采信
    pipeline.llm.chat_json = lambda messages, **kw: {"text": "短", "fixes": [{"wrong": "x", "right": "y"}]}
    try:
        same2, nofix2 = pipeline._proofread("一段足够长的原始转录文本，用来验证长度护栏是否生效")
    finally:
        pipeline.llm.chat_json = orig_json
    print(("[ok] " if (nofix2 == [] and "足够长" in same2) else "[FAIL] ")
          + "缩减过度时不采信")

    shutil.rmtree(tmp, ignore_errors=True)
    print("\n" + ("ASR 链路通过" if bad == 0 else f"{bad} 项失败"))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
