"""语音画像：判断这段音频是什么话、谁在说、说得清不清楚。

为什么要做这件事：DeepSeek ASR 的接口不返回语种，也不返回置信度 ——
官方文档明说「语言从音频自动检测」，但响应里只有 text 与 duration_ms。
所以拿不到「这是什么话」这个信息，只能自己算。

两条独立证据：
  1. 声学（读 WAV 波形算）：语速、静音占比、能量起伏、基频波动
     → 区分「机器朗读/专业配音」与「真人自然讲述」
  2. 文本（读转录算）：字符集、填充词、重复片段、方言词
     → 区分语种与方言倾向

结论一律带证据、带保留。宁可说「疑似」，不能给出一个读者会当真的标签。
"""
from __future__ import annotations

import math
import re
import wave
from pathlib import Path

try:
    import numpy as np
    from numpy.lib.stride_tricks import as_strided
except ImportError:  # pragma: no cover
    np = None
    as_strided = None

# 各地方言的高辨识度用词。只收「单独出现就基本能定位」的，
# 像「啥」这种全国通用的不收，否则满篇误报。
DIALECT_WORDS = {
    "西南（川渝云贵）": ("晓得", "啥子", "莫得", "巴适", "咋个", "要得", "啷个", "安逸"),
    "东北": ("俺们", "咋整", "嘎哈", "整明白", "贼拉", "嘚瑟"),
    "吴（江浙沪）": ("阿拉", "侬", "勿要", "阿是", "迭个", "拎勿清"),
    "粤（两广）": ("咩", "嗰", "嘅", "唔", "係", "睇", "识得", "搞掂"),
    "闽": ("厝", "阮", "汝", "即马", "古早"),
    "西北": ("谝", "尕", "咋咧", "嫑"),
}

# 口语里的「思考声」，TTS 与念稿几乎没有
FILLERS = ("嗯", "呃", "啊", "哦", "唉", "那个", "这个", "就是说", "对吧",
           "然后", "其实吧", "怎么说", "反正")

_CJK = re.compile(r"[\u4e00-\u9fff]")
_EN = re.compile(r"[A-Za-z]")


def _read_wav(wav_path: str | Path):
    """读一个 WAV。16kHz 单声道 16bit 是 ASR 切片的固定格式。"""
    try:
        with wave.open(str(wav_path), "rb") as wf:
            sr = wf.getframerate()
            ch = wf.getnchannels()
            sw = wf.getsampwidth()
            raw = wf.readframes(wf.getnframes())
    except (wave.Error, OSError) as exc:
        raise ValueError(f"读不出音频：{exc}") from exc
    if sw != 2:
        raise ValueError(f"采样位宽 {sw*8}bit，只处理 16bit")
    x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return x, sr


def _acoustic(wav_path: str | Path) -> dict:
    if np is None:
        return {"ok": False, "why": "numpy 未安装"}
    try:
        x, sr = _read_wav(wav_path)
    except ValueError as exc:
        return {"ok": False, "why": str(exc)}
    return _analyze(x, sr)


def _acoustic_many(paths: list, max_parts: int = 4) -> dict:
    """把多片拼起来算。

    ASR 会把长音频切成 110 秒一片，只分析第一片会让「语速」的分母
    只有 110 秒、分子却是全篇字数 —— 实测算出过 22.5 字/秒这种离谱值。
    """
    if np is None:
        return {"ok": False, "why": "numpy 未安装"}
    xs, sr = [], None
    for p in list(paths)[:max_parts]:
        try:
            x, sr = _read_wav(p)
        except ValueError as exc:
            return {"ok": False, "why": str(exc)}
        xs.append(x)
    if not xs:
        return {"ok": False, "why": "没有可用音频"}
    return _analyze(np.concatenate(xs), sr)


def _analyze(x, sr) -> dict:
    if x.size == 0:
        return {"ok": False, "why": "音频为空"}
    dur = x.size / sr
    win, hop = int(0.025 * sr), int(0.010 * sr)
    n = max(1, (x.size - win) // hop + 1)
    # 用步长视图取重叠帧，不物化索引矩阵与帧副本。旧写法 idx(int64, n×win)
    # + frames(float32, n×win) + frames**2 三份临时数组，长视频切成 3 片时峰值
    # 冲上 ~210MB，撞破容器 256MB 上限被 OOM 杀掉（前端就卡在「转写中」）。
    frames = as_strided(x, shape=(n, win),
                        strides=(hop * x.strides[0], x.strides[0]),
                        writeable=False)
    rms = np.sqrt((frames ** 2).mean(axis=1) + 1e-12)

    # 静音阈值。不能用「低分位 × 常数」—— 当静音段不到 15% 时，低分位本身
    # 就是正常说话的音量，乘上系数会把整段判成静音（实测踩过：一段规整的
    # 合成音被判「几乎整段静音」）。改成先看动态范围：
    lo, hi = float(np.percentile(rms, 5)), float(np.percentile(rms, 95))
    if hi <= lo * 1.5:
        # 起伏很小（合成音/强降噪），说明没有可分的静音段，用绝对比例
        thr = max(hi * 0.30, 1e-4)
    else:
        thr = max(lo + (hi - lo) * 0.15, 1e-4)
    voiced = rms > thr
    # 5 帧中值滤波：能量在阈值附近抖动会把一次停顿拆成十几段
    if voiced.size >= 5:
        v = voiced.astype(np.int8)
        padded = np.pad(v, 2, mode="edge")
        voiced = np.array([np.median(padded[i:i + 5]) > 0.5
                           for i in range(v.size)], dtype=bool)
    voiced_ratio = float(voiced.mean())
    if voiced.sum() < 20:
        return {"ok": False, "why": "几乎整段静音，无法分析"}

    vr = rms[voiced]
    energy_cv = float(vr.std() / (vr.mean() + 1e-9))     # 起伏：真人高，念稿低

    # 基频：对部分有声帧做自相关，找 60–400Hz 的峰
    pitches = []
    vf = frames[voiced]
    step = max(1, len(vf) // 400)
    lo, hi = int(sr / 400), int(sr / 60)
    for fr in vf[::step]:
        fr = fr - fr.mean()
        ac = np.correlate(fr, fr, mode="full")[len(fr) - 1:]
        if ac[0] <= 0:
            continue
        seg = ac[lo:hi]
        if seg.size == 0:
            continue
        k = int(np.argmax(seg)) + lo
        if ac[k] > 0.3 * ac[0]:
            pitches.append(sr / k)
    pitch_cv = float(np.std(pitches) / (np.mean(pitches) + 1e-9)) if len(pitches) > 30 else None

    # 停顿长度分布。次数不管用（TTS 的句间停顿一样多），
    # 管用的是「停顿长短是否整齐」—— 合成音固定 0.3 秒，真人从 0.1 到 1 秒乱来。
    v = voiced.astype(np.int8)
    edges = np.diff(np.concatenate(([0], v, [0])))
    starts = np.where(edges == 1)[0]
    ends = np.where(edges == -1)[0]
    pauses = (starts[1:] - ends[:-1]) * hop / sr if len(starts) > 1 else np.array([])
    pauses = pauses[pauses > 0.03]      # 忽略帧级别的抖动
    pause_mean = float(pauses.mean()) if pauses.size else 0.0
    pause_cv = (float(pauses.std() / pauses.mean())
                if pauses.size >= 3 and pauses.mean() > 0 else None)

    return {
        "ok": True,
        "duration": dur,
        "sr": sr,
        "voiced_ratio": voiced_ratio,
        "energy_cv": energy_cv,
        "pitch_cv": pitch_cv,
        "pause_count": int(pauses.size),
        "pause_mean": pause_mean,
        "pause_cv": pause_cv,
        "rms_median": float(np.median(vr)),
    }


def _textual(text: str) -> dict:
    cjk = len(_CJK.findall(text))
    en = len(_EN.findall(text))
    total_letters = cjk + en
    fillers = sum(text.count(w) for w in FILLERS)
    dialects = {}
    for name, words in DIALECT_WORDS.items():
        hits = [w for w in words if w in text]
        if hits:
            dialects[name] = hits
    # 自我修正/结巴：「A，不对，B」「就是说」式回退，念稿不会有
    repair = len(re.findall(r"(?:不对|应该是|我是说|或者说|重说)", text))
    return {
        "cjk": cjk,
        "en": en,
        "cjk_ratio": cjk / total_letters if total_letters else 0.0,
        "fillers": fillers,
        "dialects": dialects,
        "repair": repair,
    }


def _conclude(a: dict, t: dict, duration: float) -> dict:
    """把两路证据合成一句人话，并明确哪些是推断、哪些是事实。"""
    out: dict = {"facts": [], "guesses": [], "risks": []}

    # ---- 语种：这条最硬，字符集不会骗人 ----
    if t["cjk_ratio"] >= 0.85:
        out["lang"] = "中文"
    elif t["cjk_ratio"] <= 0.15 and t["en"] > 0:
        out["lang"] = "非中文（以拉丁字母为主）"
    elif t["cjk"] and t["en"]:
        out["lang"] = "中英混杂"
    else:
        out["lang"] = "无法判断"
    out["facts"].append(f"汉字 {t['cjk']} 个、拉丁字母 {t['en']} 个"
                        f"（汉字占比 {t['cjk_ratio']*100:.0f}%）")

    # ---- 方言：只有命中高辨识度词才说，否则明确写「未检出」 ----
    if t["dialects"]:
        name, hits = max(t["dialects"].items(), key=lambda kv: len(kv[1]))
        out["dialect"] = f"疑似{name}方言"
        out["guesses"].append(f"命中方言用词 {len(hits)} 个：{'、'.join(hits[:4])}")
        out["risks"].append("方言口音会显著抬高 ASR 同音错字率，逐字核的成本要加")
    else:
        out["dialect"] = "未检出方言用词（倾向普通话）"
        out["facts"].append("未检出川渝/东北/吴/粤/闽/西北的方言标志词")

    # ---- 说话方式：只能给「倾向」，因为 TTS 与专业配音在声学上很像 ----
    if not a.get("ok"):
        out["style"] = "无法判断（音频分析不可用）"
        out["guesses"].append(a.get("why", ""))
        return out

    chars_per_voiced_sec = t["cjk"] / max(a["duration"] * a["voiced_ratio"], 1e-9)
    out["facts"].append(f"语速 {chars_per_voiced_sec:.1f} 字/秒（按有声时长算）")
    out["facts"].append(f"有声占比 {a['voiced_ratio']*100:.0f}%，"
                        f"音量起伏 {a['energy_cv']:.2f}")
    if a.get("pitch_cv"):
        out["facts"].append(f"基频波动 {a['pitch_cv']:.2f}")
    if a.get("pause_cv") is not None:
        out["facts"].append(f"停顿 {a['pause_count']} 段，平均 {a['pause_mean']:.2f} 秒，"
                            f"长短离散度 {a['pause_cv']:.2f}")

    # 三个指标投票：念稿/合成音通常「起伏小、填充词少、停顿规整」
    votes = 0
    if a["energy_cv"] < 0.30:
        votes += 1
    if t["fillers"] <= 2:
        votes += 1
    if a.get("pause_cv") is not None and a["pause_cv"] < 0.35:
        votes += 1          # 停顿长短过于整齐
    if a.get("pitch_cv") is not None and a["pitch_cv"] < 0.15:
        votes += 1

    if votes >= 3:
        out["style"] = "朗读腔（机器合成或专业配音）"
        out["guesses"].append("音量起伏小、填充词少、停顿长短整齐、音高平 —— "
                              "多项同时成立，更像照稿念或 TTS，不像即兴讲述")
        out["risks"].append("念稿/合成音发音标准，ASR 错字通常很少；"
                            "但这类内容往往是「别人写的稿」，说话人未必理解，"
                            "观点归属要看账号主体而非讲述者")
    elif votes <= 1:
        out["style"] = "真人自然讲述"
        out["guesses"].append("音量起伏大、有口语填充词、停顿不规整 —— 更像即兴讲述")
        out["risks"].append("即兴讲述的断句粘连、吞字、自我修正最多，"
                            "ASR 错字率通常高于念稿")
    else:
        out["style"] = "倾向真人（拿不准）"
        out["guesses"].append("指标互相矛盾，不敢断言是念稿还是即兴")
        out["risks"].append("说话方式不确定，转录可信度按中等估")

    if t["fillers"]:
        out["facts"].append(f"口语填充词 {t['fillers']} 处")
    if t["repair"]:
        out["facts"].append(f"自我修正/回退 {t['repair']} 处")
    return out


def combine(acoustic: dict | None, text: str, duration: float = 0.0) -> dict:
    """把已经算好的声学结果与转录文本合起来。"""
    a = dict(acoustic or {"ok": False, "why": "没有可用音频"})
    a.setdefault("duration", duration or 0.0)
    if not a.get("duration") and duration:
        a["duration"] = duration
    return _conclude(a, _textual(text or ""), a.get("duration") or duration)


def analyze(wav_path: str | Path | None, text: str, duration: float = 0.0) -> dict:
    """一步到位版。音频拿不到时仍给文本结论，不让整条流程失败。"""
    a = _acoustic(wav_path) if wav_path else {"ok": False, "why": "没有可用音频"}
    return combine(a, text, duration)
