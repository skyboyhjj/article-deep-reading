#!/usr/bin/env python3
"""语音画像的回归用例。

用合成音频做两侧对照：平稳振幅 + 规整停顿（像 TTS/配音）与
起伏振幅 + 不规则停顿（像真人即兴）。不追求判准真实语音，
只保证「指标方向没写反、结论带保留、缺音频不崩」。
"""
import os
import sys
import tempfile
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import voice  # noqa: E402

SR = 16000


def write_wav(path: str, x: np.ndarray) -> None:
    x = np.clip(x, -1, 1)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes((x * 32767).astype("<i2").tobytes())


def tts_like(sec: float = 20.0) -> np.ndarray:
    """像合成音：振幅几乎不变、句子之间停顿规整、音高平。"""
    t = np.arange(int(SR * sec)) / SR
    seg, out = [], []
    for _ in range(int(sec // 2)):
        s = 1.7  # 每句 1.7 秒，中间静音 0.3 秒
        tt = np.arange(int(SR * s)) / SR
        tone = 0.5 * np.sin(2 * np.pi * 180 * tt)          # 单一基频
        tone += 0.2 * np.sin(2 * np.pi * 360 * tt)
        seg.append(tone * 0.6)
        seg.append(np.zeros(int(SR * 0.3)))
    out = np.concatenate(seg)[: t.size]
    return out * 0.9           # 全程同一音量


def human_like(sec: float = 20.0) -> np.ndarray:
    """像真人：音量忽大忽小、停顿长短不一、音高有起伏。"""
    rng = np.random.default_rng(7)
    out = []
    total = 0
    while total < sec * SR:
        dur = rng.uniform(0.4, 2.2)
        tt = np.arange(int(SR * dur)) / SR
        f0 = rng.uniform(110, 240) * (1 + 0.15 * np.sin(2 * np.pi * 0.7 * tt))
        amp = rng.uniform(0.15, 0.95)
        seg = amp * (np.sin(2 * np.pi * f0 * tt) + 0.4 * rng.standard_normal(tt.size))
        out.append(seg)
        total += seg.size
        pause = rng.choice([0, 0, int(SR * rng.uniform(0.05, 1.4))])
        out.append(np.zeros(pause))
        total += pause
    x = np.concatenate(out)[: int(sec * SR)]
    return x * 0.9


def main() -> int:
    bad = 0

    def chk(name, cond):
        nonlocal bad
        print(("[ok] " if cond else "[X ] ") + name)
        if not cond:
            bad += 1

    tmp = tempfile.mkdtemp()
    p_tts = os.path.join(tmp, "tts.wav")
    p_hum = os.path.join(tmp, "human.wav")
    write_wav(p_tts, tts_like())
    write_wav(p_hum, human_like())

    a_tts = voice._acoustic(p_tts)
    a_hum = voice._acoustic(p_hum)
    chk("合成音能读", a_tts.get("ok") is True)
    chk("人声能读", a_hum.get("ok") is True)
    chk("合成音音量更平（energy_cv 更小）",
        a_tts["energy_cv"] < a_hum["energy_cv"])
    chk("合成音停顿更整齐（pause_cv 更小）",
        (a_tts["pause_cv"] or 0) < (a_hum["pause_cv"] or 1))
    chk("合成音音高更平（pitch_cv 更小）",
        (a_tts["pitch_cv"] or 0) < (a_hum["pitch_cv"] or 1))

    # 文本侧
    zh = "这个事儿吧，我觉得就是说，芯片这东西嗯，不是一天能做出来的。"
    t_zh = voice._textual(zh)
    chk("识别人话填充词", t_zh["fillers"] >= 3)
    chk("汉字占比高", t_zh["cjk_ratio"] > 0.9)
    en = "The fabless model means NVIDIA does not own fabs at all."
    chk("英文汉字占比低", voice._textual(en)["cjk_ratio"] < 0.1)
    sw = "晓得嘛，这个啥子东西，莫得办法的，巴适得很"
    chk("方言词命中", "西南（川渝云贵）" in voice._textual(sw)["dialects"])

    # 合成结论
    r_tts = voice.combine(a_tts, zh)
    chk("合成音+少填充 → 朗读腔", "朗读" in r_tts["style"])
    r_hum = voice.combine(a_hum, zh)
    chk("人声+多填充 → 真人", "真人" in r_hum["style"])
    chk("结论分实测与推断两栏",
        bool(r_tts.get("facts")) and bool(r_tts.get("guesses")))
    chk("一定有对转录的影响提示", bool(r_tts.get("risks")))

    # 边界：没有音频
    r_no = voice.analyze(None, zh)
    chk("无音频不崩", isinstance(r_no.get("style"), str))
    chk("无音频仍给语种", r_no.get("lang") == "中文")
    # 边界：坏文件
    p_bad = os.path.join(tmp, "bad.wav")
    with open(p_bad, "wb") as fh:
        fh.write(b"not a wav")
    chk("坏文件不崩", voice._acoustic(p_bad).get("ok") is False)

    print("\n" + ("语音画像通过" if bad == 0 else f"{bad} 项失败"))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
