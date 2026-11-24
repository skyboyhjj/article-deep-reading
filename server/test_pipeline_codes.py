#!/usr/bin/env python3
"""流水线错误路径的行为用例（P3 补齐）。

来由：4001 / 4003 / 4004 / 4005 / 4006 这五条码以前只有码表里的定义，
分支代码也写了，但没有任何测试真的跑到过——`test_codes.py` 里那几行只是
「异常类 ↔ 码」的映射断言，不等于触发。守门三（每个码至少一个触发用例）
要的是后者，所以这里把五条路径各跑一次。

顺带修掉一处合并：`media.MediaError`（文件读不出来）原先和 `asr.ASRError`
（转写失败）一起报 DR-4004，导致 DR-4005 是条永远走不到的码。两者用户的
动作不同——一个换格式，一个等等再试——现在分开。

跑法：python3 server/test_pipeline_codes.py
"""

from __future__ import annotations

import sys

import asr
import extract
import llm
import media
import pipeline

FAILS: list[str] = []


def chk(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'ok' if cond else '!!'}] {name}"
          + (f"  {detail}" if not cond and detail else ""))
    if not cond:
        FAILS.append(name)


def first_code(events: list[dict]) -> tuple[str | None, dict]:
    """取第一个 fail 帧的码。顺便把整帧返回，后面查 stage。"""
    for ev in events:
        if ev.get("status") == "fail":
            return ev.get("code"), ev
    return None, {}


class patch:
    """临时替换模块属性，退出时还原。

    不用 unittest.mock 是因为这几个目标都是模块级函数，直接赋值最好读；
    但必须还原，否则后面的用例会踩到前一个的假实现。
    """

    def __init__(self, obj, **kw):
        self.obj, self.kw, self.old = obj, kw, {}

    def __enter__(self):
        self.old = {k: getattr(self.obj, k) for k in self.kw}
        for k, v in self.kw.items():
            setattr(self.obj, k, v)
        return self

    def __exit__(self, *exc):
        for k, v in self.old.items():
            setattr(self.obj, k, v)
        return False


def boom(exc):
    def _raise(*a, **k):
        raise exc
    return _raise


PASSED_TEXT = "这是一段用于触发无输入校验的正文。" * 20   # > 200 字


print("=" * 70)
print("流水线错误路径：每个码都要真的走到")
print("=" * 70)

# ---- DR-4001 模型未配置 ---------------------------------------------------
with patch(llm, is_configured=lambda: False):
    ev = list(pipeline.analyze("", PASSED_TEXT, "", "", ""))
code, frame = first_code(ev)
chk("DR-4001 模型未配置 → extract 阶段失败", code == "DR-4001", f"实际 {code}")
chk("DR-4001 挂在 extract 阶段", frame.get("stage") == "extract", str(frame.get("stage")))

# ---- DR-4003 链接抓不到 --------------------------------------------------
with patch(extract, fetch=boom(extract.FetchError("DNS 解析失败"))):
    ev = list(pipeline.analyze("https://example.com/nope", "", "", "", ""))
code, frame = first_code(ev)
chk("DR-4003 抓取失败（无备选媒体）→ extract 阶段失败", code == "DR-4003", f"实际 {code}")

# 抓取失败但有上传件时**不**报错，改为降级继续 —— 这条是 v2.27 的行为
with patch(extract, fetch=boom(extract.FetchError("DNS 解析失败"))), \
        patch(llm, is_configured=lambda: True, chat_json=boom(RuntimeError("stub")),
              chat=lambda *a, **k: ""):
    ev = list(pipeline.analyze("https://example.com/nope", "", "", "/tmp/fake.mp4", "a.mp4"))
code, frame = first_code(ev)
chk("DR-4003 有上传件时不报错（降级）", code != "DR-4003", f"实际 {code}")

# ---- DR-4006 转写未配置 --------------------------------------------------
with patch(asr, is_configured=lambda: False, describe=lambda: "未配置 ASR_BACKEND"):
    ev = list(pipeline.analyze("", "", "", "/tmp/fake.mp4", "a.mp4"))
code, frame = first_code(ev)
chk("DR-4006 转写未配置 → transcribe 阶段失败", code == "DR-4006", f"实际 {code}")
chk("DR-4006 挂在 transcribe 阶段", frame.get("stage") == "transcribe", str(frame.get("stage")))

# ---- DR-4005 文件读不出来 ------------------------------------------------
with patch(asr, is_configured=lambda: True, describe=lambda: "stub",
           audio_spec=lambda: {"max_sec": 600, "fmt": "mp3", "why": "stub"}), \
        patch(media, prepare=boom(media.MediaError("容器损坏"))):
    ev = list(pipeline.analyze("", "", "", "/tmp/fake.mp4", "a.mp4"))
code, frame = first_code(ev)
chk("DR-4005 文件读不出来（不是 4004）", code == "DR-4005", f"实际 {code}")

# ---- DR-4004 转写失败 ----------------------------------------------------
PREP = {"parts": ["/tmp/part1.mp3"], "duration": 30.0, "seg_sec": 30}
with patch(asr, is_configured=lambda: True, describe=lambda: "stub",
           audio_spec=lambda: {"max_sec": 600, "fmt": "mp3", "why": "stub"},
           transcribe_parts=boom(asr.ASRError("上游 502"))), \
        patch(media, prepare=lambda *a, **k: dict(PREP)):
    ev = list(pipeline.analyze("", "", "", "/tmp/fake.mp4", "a.mp4"))
code, frame = first_code(ev)
chk("DR-4004 转写失败", code == "DR-4004", f"实际 {code}")

print()
print("=" * 70)
if FAILS:
    print(f"失败 {len(FAILS)} 项：")
    for f in FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("流水线错误路径用例通过")
