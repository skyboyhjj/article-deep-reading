"""编排层：把七步 SOP 压成五个可流式输出的阶段。

设计原则（两条，都是踩过坑得来的）：
  1. **数字只从实算记录流入报告。** 报告由确定性模板拼装，LLM 只写不含数字的评述。
     让 LLM 复述数字，等于把刚算准的数交给它改写——这是档 2 最容易被自己做坏的地方。
  2. **降级写在脸上。** 抓不到全文、算不出来、核不到原始来源，都要在报告里显式标
     「未核」「未实算」，不许含糊过去。

每个阶段 yield 一个事件 dict，由 app.py 转成 SSE：
    {"stage": "...", "status": "run|ok|warn|fail", "text": "...", "data": {...}}
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from typing import Generator

import asr
import extract
import llm
import media
import search
import voice
from safe_eval import EvalError, UnsafeExpression, evaluate, has_operation, has_variable

MAX_CHARS = 24000      # 送模型的正文上限
MAX_CLAIMS = 8         # 核验项上限
MAX_CALCS = 5          # 实算项上限

SYSTEM = """你是一名严格的文章读解员，任务是把一篇文章拆开核验，不是写摘要。

铁律：
- 不写摘要、不复述原文、不吹捧作者。
- 只输出要求的结构化内容，不要加解释性开场白。
- 拿不到证据就说「未核」，不要猜。
- **你的内部知识不算证据**。涉及具体参数、公开数字、机构头衔、经典引文时，
  只能判「未核」并写明该去查什么；除非正文自身前后矛盾（那属「不实」）。
- 必须能指出「它讲对了什么」，只挑错的读解没有公信力。
"""


def _clip(text: str, n: int = MAX_CHARS) -> str:
    return text if len(text) <= n else text[:n] + "\n…（正文已截断）"


# 模型常在 expected 里自述「原文未给」，那就等于没有对照值 —— 这类必须挡住
_NEG_EXPECT = ("未给", "没给", "没有", "未核", "未说明", "未明说", "无对照", "外部核验",
               "无法确定", "不可比", "未提供", "尚无", "未披露", "未提及", "未明确",
               "仅给", "仅有两个数值", "待核")


_NEG_EXPECT_RE = re.compile(r"未.{0,4}(给|说|明|提供|披露|提及|直接给出)")

# 量化表述：数字分数、汉字分数、倍数、成数。用来核对「声称值」是不是原文里的话。
_QUANT_RE = re.compile(r"\d+\s*/\s*\d+|三分之一|三分之二|四分之一|四分之三|五分之一|"
                       r"一半|减半|翻[倍番]|\d+\s*倍|[一二三四五六七八九十]成|\d+\s*成")
_QUANT_NORM = {
    "三分之一": "1/3", "三分之二": "2/3", "四分之一": "1/4", "四分之三": "3/4",
    "五分之一": "1/5", "一半": "1/2", "减半": "1/2", "翻倍": "2", "翻番": "2",
    "一成": "0.1", "两成": "0.2", "三成": "0.3", "四成": "0.4", "五成": "0.5",
    "六成": "0.6", "七成": "0.7", "八成": "0.8", "九成": "0.9",
}


def _norm_quant(text: str) -> str:
    t = re.sub(r"\s+", "", text or "")
    for k, v in _QUANT_NORM.items():
        t = t.replace(k, v)
    return t


def _expected_in_body(expected: str, body: str) -> tuple[bool, str]:
    """声称值里的量化表述，原文里到底有没有。

    v2.1 实况：原文只说「3.3% 和 2.1%」，模型却在 expected 里写「下降的1/3」——
    那是它自己算出来的结论，冒充「原文声称」，于是实算 0.364 被判「不符」。
    这不是原文错，是模型给自己造了个靶子再打。
    """
    if not body:
        return True, ""
    nb = _norm_quant(body)
    for m in _QUANT_RE.finditer(expected or ""):
        frag = _norm_quant(m.group(0))
        if frag and frag not in nb:
            return False, f"声称值里的「{m.group(0)}」在原文找不到（多半是模型自己算的）"
    return True, ""


def _claim_usable(expected: str, body: str = "") -> tuple[bool, str]:
    text = (expected or "").strip()
    if not text:
        return False, "原文没有可对照的声称值"
    ok, why = _expected_in_body(text, body)
    if not ok:
        return False, why
    for word in _NEG_EXPECT:
        if word in text:
            return False, f"声称值自述「{word}」，等于没有对照值"
    # 「未直接给出倍数」这种换了个说法的，词表列不全，用正则兜住
    m = _NEG_EXPECT_RE.search(text)
    if m:
        return False, f"声称值自述「{m.group(0)}」，等于没有对照值"
    return True, ""


def _symbols_note(syms: dict, text: str) -> str:
    """symbols 里有没有原文找不到的数值（可能是模型自己引入的外部数据）。"""
    if not syms:
        return ""
    missing = []
    for k, v in syms.items():
        forms = {str(v)}
        try:
            fv = float(v)
            if fv.is_integer():
                forms.add(str(int(fv)))
            # 原文写「5万」、symbols 写 50000 —— 这是同一个数，不该报成「找不到」
            for unit, scale in (("亿", 1e8), ("万", 1e4), ("千", 1e3)):
                q = fv / scale
                if q.is_integer() and 1 <= q < 1000:
                    forms.add(f"{int(q)}{unit}")
        except (TypeError, ValueError):
            pass
        if not any(f in text for f in forms):
            missing.append(f"{k}={v}")
    if not missing:
        return ""
    return ("；其中 " + "、".join(missing[:3]) +
            " 在原文找不到对应写法（可能是中文数字，也可能是模型引入的外部数据）")


# 「用正文证明正文」的典型说法 —— 出现在「确证」的依据里就说明这条没被核过
_SELF_PROOF = ("正文自身", "正文自述", "正文自己", "正文表述", "正文里称",
               "与说法一致", "属公开常识", "公开常识性事实", "据正文")

_CN_UNIT = {"百": 1e2, "千": 1e3, "万": 1e4, "亿": 1e8, "倍": 1.0}


def _as_number(text: str) -> float | None:
    """把「0.75」「3/4」「75%」「5万」这类声称值解析成数，用于自动比对；解析不了返回 None。"""
    t = (text or "").strip().replace(",", "").replace("，", "")
    # 「成本直接翻倍」—— 倍数只有汉字没有数字（要在归一化之前判，否则被换成 "2"）
    if "翻倍" in t or "翻番" in t or "翻了一倍" in t:
        return 2.0
    # 「三分之一」「减了一半」「三成」先归一成数字写法，再走下面的解析
    t = _norm_quant(t)
    # 分数：「约1/6」「头发丝的1/3」——原文常把比例写成这种形式
    m = re.search(r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)", t)
    if m:
        den = float(m.group(2))
        return float(m.group(1)) / den if den else None
    # 「每秒钟打5万次」里的 5万 —— 中文单位是 ASR 与科普口播的常态
    m = re.search(r"([-+]?\d*\.?\d+)\s*([百千万亿倍])", t)
    if m:
        try:
            return float(m.group(1)) * _CN_UNIT[m.group(2)]
        except ValueError:
            pass
    if not t:
        return None
    if t.endswith("%"):
        try:
            return float(t[:-1]) / 100.0
        except ValueError:
            return None
    if "/" in t:
        a, _, b = t.partition("/")
        try:
            den = float(b)
            return float(a) / den if den else None
        except ValueError:
            return None
    try:
        return float(t)
    except ValueError:
        return None


_SELF_DUP_RE = re.compile(r"「([^」]{1,30})」\s*(?:→|->)?\s*需?要?确认原文是否写作「\1」")


_FIX_NUM = re.compile(r"\d|万|千|亿|成")
_FIX_LATIN = re.compile(r"[A-Za-z]{2,}")
_FIX_PUNCT = "。，、；：！？"
# 依据够硬的几种：用户预设的规则替换、上下文讲不通、与别处不一致、标准译名。
# 有这些托底，改对率高，不必让读者回听。
_FIX_STRONG = ("规则替换", "上下文", "语义", "不通", "前后", "标准译名", "与别处")


def _fix_risk(fix: dict) -> tuple[str, str]:
    """给一条修正判「类别」与「建议」。

    两件事要分开：**改错的后果**与**改错的可能**。
    - 类别看后果：数字、专名改错会让后续核验建立在错前提上；断句标点改错无关痛痒。
    - 建议看托底：「规则替换」「上下文讲不通」这类依据硬，可直接采用；
      只说了「同音」而没给上下文证据的，才建议回听。
    两者合起来才决定这一条要不要进「需要回听」。
    """
    w = str(fix.get("wrong", ""))
    r = str(fix.get("right", ""))
    why = str(fix.get("why", ""))
    blob = w + r

    if _FIX_NUM.search(blob):
        kind = "数字"
    elif _FIX_LATIN.search(blob):
        kind = "专名"
    elif any(p in blob for p in _FIX_PUNCT):
        kind = "断句"
    elif len(w) == len(r) and w != r and sum(1 for a, b in zip(w, r) if a != b) == 1:
        kind = "同音字"
    elif len(r) > len(w):
        kind = "补字"
    elif len(r) < len(w):
        kind = "删字"
    else:
        kind = "改写"

    strong = any(k in why for k in _FIX_STRONG)
    if kind in ("数字", "专名"):
        return (kind, "可接受" if strong else "引用前回听")
    if kind == "同音字":
        return (kind, "可接受" if strong else "术语/人名请回听")
    return (kind, "可接受")


def _risk_rank(row) -> int:
    text = (str(row.get("as_is", "")) + str(row.get("why", ""))
            if isinstance(row, dict) else str(row))
    if "应为" in text or "误识" in text or "误写" in text or "错字" in text:
        return 0
    if "确认" in text or "核对" in text or "需查" in text:
        return 1
    return 2


def _drop_self_duplicates(risks: list) -> list:
    """丢掉「A 而非 A」这种零信息条目。

    v1.8 实况：模型列了 55 条转述偏差，其中「「弯道超车」→ 需确认是否写作
    「弯道超车」而非「弯道超车」」这种同字重复占了一大半 —— 那是把每个词抄一遍，
    不是发现。真正的错字在「转录校对」节里，这里只留它挑得出的。
    """
    kept = []
    for r in risks:
        if isinstance(r, dict):
            a = str(r.get("as_is", "")).strip()
            b = str(r.get("should_be", "")).strip()
            if b and a and a == b:
                continue
            if b and a and a in b and len(b) <= len(a) + 4:
                continue
        # 合并成一整句的形态：从文本里找「「A」…「B」」是否同一个词
        text = str(r.get("as_is", "") if isinstance(r, dict) else r)
        m = re.search(r"「([^」]{1,30})」[^「]{0,40}需确认原文是否写作「([^」]{1,30})」", text)
        if m and m.group(1) == m.group(2):
            continue
        kept.append(r)
    return kept


def _body(doc: dict) -> str:
    """核验与实算只针对原话：有转录就用转录，避免拿元宝总结当原文去核。"""
    return doc.get("transcript") or doc["text"]


def _trim(v: str, n: int = 120) -> str:
    v = re.sub(r"\s+", " ", str(v)).strip()
    return v if len(v) <= n else v[:n] + "…"


def video_extra(doc: dict) -> str:
    """视频类材料的额外任务。L0 原声与 L1 二手总结必须分开说 ——
    对原声转录讲「你拿到的是元宝总结」是假的，模型会因此不去查 ASR 错字。"""
    if doc.get("transcript"):
        return (
            "\n\n注意：本次是「原声转录（L0）」，不是元宝总结。但转录由 ASR 生成，"
            "必然有错字，同音字、数字误识、断句粘连是重灾区。"
            "（实例：「几十万」被识成「几10万」、「极紫外」识成「及紫外」、"
            "「托住」识成「拖住」、「当你」识成「你」）\n"
            "因此必须额外给出三项：\n"
            "1) who_speaks：一句话说清「账号是谁、讲述者是谁、是否同一人」，取不到就写未核；\n"
            "2) transcript_risks：**只列你有具体理由怀疑写错的**，最多 6 条。"
            "每条给「原文写的 → 应该是 → 怀疑理由」，理由必须是这三类之一："
            "①同音/近音对（如「极紫外」被识成「及紫外」）；"
            "②上下文语义不通（如「才拖住了这颗硅片」讲不通，应为「托住」）；"
            "③与文中别处写法不一致。\n"
            "    找不出具体理由的一律不列。把「台积电」写成「可能被识别成台机电」不算发现，"
            "那是噪音 —— 读者要的是「哪里真错了」，不是「每个词都可能错」。"
            "一个字都挑不出就返回空数组，不要凑数；\n"
            "3) title_claims：正文里声明的身份与头衔，逐条给「可核 / 未核」。\n"
            "以上都是数组或字符串，没有就给空数组。\n"
        )
    return (
        "\n\n注意：本次来源是「二手」——你拿到的是元宝对一段视频的总结，不是视频原话。"
        "因此必须额外给出三项：\n"
        "1) who_speaks：一句话说清「账号是谁、讲述者是谁、是否同一人」；\n"
        "2) transcript_risks：疑似听写/转述偏差的表述，逐条给「原文写的 → 应该是 → 依据」。"
        "中文同音术语是重灾区（如「七损八益」被写成「七胜八益」、「百分之」听成「百分只」），"
        "凡属经典引文、专有名词、数字，一律检查；\n"
        "3) title_claims：文案里声明的身份与头衔，逐条给「可核 / 未核」。\n"
        "以上都是数组或字符串，没有就给空数组。\n"
    )


SUMMARY_EXTRA = (
    "\n\n本次材料里同时有「视频原声转录」与「元宝总结」。请额外做『总结忠实度』核对，"
    "给出 summary_check 数组，每条：\n"
    '  {"point": "元宝总结的一个要点（不超过30字）", '
    '"in_transcript": "有 | 部分 | 无", '
    '"verdict": "忠实 | 省略 | 添加 | 歪曲", '
    '"evidence": "转录里的原话片段（不超过40字），没有就写空"}\n'
    "重点看四类走形：把条件句说成断言、把某人的观点说成经典原文、"
    "数字与量级对不上、把否定说成肯定。只挑能影响结论的，最多 6 条。\n"
)


def _json_with_retry(messages: list, max_tokens: int, tight: str, **kw) -> tuple[dict, bool]:
    """调一次；解析失败就追加一句「精简输出」再来一次。

    模型输出被截断是概率事件，重试时把要求写死（条目更少、理由更短），
    比原样重试有效得多。返回 (结果, 是否用了重试)。
    """
    try:
        return llm.chat_json(messages, max_tokens=max_tokens, **kw), False
    except llm.LLMError:
        msgs = list(messages) + [{"role": "user", "content": tight}]
        return llm.chat_json(msgs, max_tokens=max_tokens, **kw), True


def stage_triage(doc: dict) -> dict:
    """判体裁 + 核作者序 + 列可核项。作者序是硬事实，先立住。"""
    layer_line = f"来源层级：{doc['layer']}\n" if doc.get("layer") else ""
    extra = video_extra(doc) if doc["kind"] in ("wechat_channel", "video_upload") else ""
    if doc.get("transcript") and doc.get("summary_text"):
        extra += SUMMARY_EXTRA
    msgs = [
            {"role": "system", "content": SYSTEM},
            {
                "role": "user",
                "content": (
                    "第一步：定性与列清单。\n"
                    f"标题：{doc['title']}\n"
                    f"抓取到的作者（按页面顺序）：{doc['authors']}\n"
                    f"发布信息：{doc['published'] or '未取到'}\n"
                    f"来源类型：{doc['kind']}\n"
                    f"{layer_line}"
                    f"正文：\n{_clip(doc['text'])}\n\n"
                    "返回 JSON：{\n"
                    '  "genre": "论文报道 | 论文科普 | 个人思辨 | 编译稿 | 视频口播",\n'
                    '  "genre_reason": "一句话理由",\n'
                    '  "author_note": "作者序是否与文中声称一致；若声称有人领衔，指出实际第一作者",\n'
                    '  "claims": [{"quote": "原文关键说法(不超过40字)", "why": "为什么值得核", '
                    '"kind": "数字 | 事实 | 引用 | 术语 | 因果"}],\n'
                    '  "who_speaks": "", "transcript_risks": [], "title_claims": [], '
                    '"summary_check": []\n'
                    "}\n"
                    f"claims 最多 {MAX_CLAIMS} 条，只挑一旦错了会改变结论的。"
                    f"{extra}"
                ),
            },
    ]
    out, retried = _json_with_retry(
        msgs, 3000,
        "上一次输出被 max_tokens 截断了。这次务必精简：claims 最多 5 条，"
        "每条 why 不超过 15 字；transcript_risks 最多 5 条，每条不超过 30 字。"
        "只输出一个完整 JSON，不要任何解释。",
    )
    if retried:
        out["_retried"] = True
    return out


def stage_verify(doc: dict, triage: dict) -> dict:
    """逐条核验。每条必须给依据或明确「未核」。"""
    claims = triage.get("claims") or []
    lines = "\n".join(f"{i+1}. [{c.get('kind','')}] {c.get('quote','')}" for i, c in enumerate(claims))
    note = ""
    if doc["kind"] == "wechat_channel":
        note = (
            "\n注意：本对象是二手总结，没有视频原话可对照。凡需外部溯源才能定的"
            "（经典引文、机构头衔、他人研究结论），一律判「未核」并写明该溯源到什么，"
            "不要凭记忆判「确证」。"
        )
    msgs = [
            {"role": "system", "content": SYSTEM},
            {
                "role": "user",
                "content": (
                    "第二步：逐条核验下列说法。\n"
                    f"正文（供比对原文用）：\n{_clip(_body(doc), 8000)}\n\n"
                    f"待核说法：\n{lines}\n\n"
                    "返回 JSON：{\"items\": [{\"n\": 1, \"verdict\": "
                    "\"确证 | 不实 | 半对 | 未核\", \"evidence\": \"依据（原文哪处/数字怎么对不上），"
                    "未核则写原因\", \"fix\": \"应改成什么\"}]}\n"
                    "注意：判「确证」必须有一条外部可指名的出处，并且写进 source（URL 或机构名）。"
                    "以下三种一律不能算「确证」：\n"
                    "（a）用正文证明正文 —— 正文说 A 所以 A 对，这是复读不是核验；\n"
                    "（b）你自己的记忆 —— 「我记得 ASML 功率是 250 瓦」不算依据；\n"
                    "（c）「这是公开常识」—— 常识也要给出出处，给不出就判「未核」。\n"
                    "只有正文自述、没有外部出处的，判「未核」，并在 fix 里写明该去查什么。"
                    f"{note}"
                ),
            },
    ]
    out, retried = _json_with_retry(
        msgs, 2600,
        "上一次输出被截断了。这次务必精简：每条的 evidence 不超过 40 字，"
        "fix 不超过 15 字；判不了的直接给「未核」，不要长篇解释。只输出完整 JSON。",
    )
    if retried:
        out["_retried"] = True
    return out


TRACE_PROMPT = (
    "下面每一项之前都判为「未核」。现在给了检索结果。请**只依据这些结果**重新判定。\n"
    "证据分三级，级别决定你能下什么结论：\n"
    "- 【权威源】：可单独支撑「确证」，evidence 写「据 X（域名）」，source 填 URL；\n"
    "- 【相关但非权威】：**不能单独支撑「确证」**。若多条互相印证且说法一致，最多判「半对」，"
    "并在 evidence 写明「多源一致，但均非权威来源」；若仅一条，保持「未核」；\n"
    "- 【仅供参考】：不参与判定。\n"
    "其他规则：\n"
    "- 有结果明确反对原说法 → 判「不实」；\n"
    "- 自媒体、门户转载、百科、问答站一律按「相关但非权威」处理，不要当权威源；\n"
    "- 结果只是互相转载同一说法（同源）不算多源印证。\n"
    '返回 JSON：{"items": [{"n": 1, "verdict": "确证 | 不实 | 半对 | 未核", '
    '"evidence": "依据", "source": "URL 或 空串"}]}'
)


# 常见非权威站点：提示词已经要求避开，这里再做一层兜底过滤
_JUNK_DOMAINS = (
    "mp.weixin.qq.com", "baijiahao.baidu.com", "baike.baidu.com", "zhihu.com",
    "sohu.com", "sina.com", "sina.cn", "163.com", "toutiao.com", "bilibili.com",
    "douyin.com", "xiaohongshu.com", "iresearch.cn", "ceweekly.cn",
)


def _clean_sites(sites) -> list[str]:
    out = []
    for s in sites or []:
        d = str(s).strip().lower()
        d = d.replace("https://", "").replace("http://", "").split("/")[0]
        if d.startswith("www."):
            d = d[4:]
        if not d or "." not in d:
            continue
        if any(j in d for j in _JUNK_DOMAINS):
            continue
        if d not in out:
            out.append(d)
    return out[:2]


def _authority_hints(pending: list[dict]) -> dict:
    """一次调用，为每个待追项给出 1–2 个「官方/权威」域名。

    只在前一批结果全是自媒体或门户时才调用，避免每次都多花一次钱。
    """
    if not pending:
        return {}
    lines = "\n".join(f"{p['n']}. {p['quote']}" for p in pending)
    try:
        out = llm.chat_json(
            [{"role": "system", "content": SYSTEM},
             {"role": "user", "content":
              "下面每条是待核说法。请给出查证它最该去的**官方或权威站点域名**"
              "（厂商官网、监管机构、标准组织、一手财报；不要给媒体、百科、问答站、资讯站）。\n"
              f"{lines}\n"
              '返回 JSON：{"items": [{"n": 1, "sites": ["example.com"]}]}；'
              "拿不准就给空数组，不要瞎猜。"}],
            max_tokens=700,
        )
    except llm.LLMError:
        return {}
    hints = {}
    for it in (out.get("items") or []):
        if not isinstance(it, dict):
            continue
        clean = _clean_sites(it.get("sites"))
        if clean:
            hints[it.get("n")] = clean
    return hints


_DOMAIN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-]*(?:\.[A-Za-z0-9\-]+)+")


def _looks_like_domain(text: str) -> bool:
    """排掉 13.5nm、0.1mm 这类被误当成域名的数字写法。"""
    parts = text.split(".")
    if len(parts) < 2:
        return False
    tld = parts[-1]
    return (tld.isalpha() and 2 <= len(tld) <= 6
            and any(c.isalpha() for c in parts[0]))


def _media_only_evidence(evidence: str) -> list:
    """依据里提到的全是媒体/门户域名，一条一手来源都没有。

    模型写依据有几种花样：「据 X（权威源）：…」「据三立新闻网（域名）报道…」
    「据 X 报道」。不管哪种，只要里面出现的域名都不是一手来源，
    这条「确证」就是拿转述当证据。一条域名都没提到的（如「据公开资料」）不判，
    因为那可能是机构简称，判了会误伤。
    """
    hosts = [h.lower() for h in _DOMAIN_RE.findall(evidence)
             if _looks_like_domain(h)]
    hosts = list(dict.fromkeys(hosts))
    if not hosts:
        return []
    if any(search.is_authoritative_host(h) for h in hosts):
        return []
    return hosts


def _no_change_fix(fix) -> bool:
    """这条 fix 是不是「不用改」。

    模型写「无需修改」，也常写「无需修改（引用与原文一致）」。后者同样是不用改，
    若降级「确证 → 半对」时按精确匹配只认前者，就会留下
    「半对：无需修改（引用与原文一致）」这种自相矛盾的行。v2.19 改成前缀判定。
    """
    s = str(fix or "").strip()
    return (not s) or s.startswith("无需修改") or s.startswith("无需改")


def enforce_evidence(items: list) -> int:
    """判「确证」却拿不出外部证据的，一律降级 —— 这道闸不放在 trace 里，
    因为 trace 只处理「未核」项，而「确证」往往在更早的核验阶段就产生了。"""
    demoted = 0
    # 「不实」是要拿相反证据说话的。fix 若还写「应查…」，说明它自己都没查清，
    # 那就只是存疑，退回「半对」。v2.1 实况：一条「组装厂」被判不实，
    # fix 却写「应查年报…再判断说法是否成立」—— 自相矛盾。
    for it in items:
        if it.get("verdict") != "不实":
            continue
        fix = str(it.get("fix") or "")
        if fix.startswith("应查") or "再判断" in fix or "是否成立" in fix:
            it["verdict"] = "半对"
            it["evidence"] = (str(it.get("evidence") or "").rstrip("。") +
                              "。（已降级为半对：判「不实」需要相反证据，"
                              "而本条依据只说「需再核实」，属于存疑而非证伪）")
            demoted += 1

    for it in items:
        if it.get("verdict") != "确证":
            continue
        tier = [x.get("tier") for x in (it.get("sources") or [])]
        ev = str(it.get("evidence") or "")

        # 模型会在依据里自己写「据 X（权威源）」。X 是不是权威由代码判，不由它说。
        # 实例：v1.6 报「据三立新闻网（rwdnews.sanlih.com.tw）报道…」判确证，
        # 三立是台湾的电视媒体 —— 是好媒体，但仍是转述，支撑不了确证。
        # 引用不存在于检索结果的来源 = 读者无从核对，不能算确证
        missing = it.get("_cites_missing") or []
        if missing:
            it["verdict"] = "半对"
            it["fix"] = ("补一条可点开的出处后再定论"
                         if _no_change_fix(it.get("fix")) else it.get("fix"))
            it["evidence"] = (ev.rstrip("。") +
                              f"。（已降级：援引的 {'、'.join(missing)} 不在本轮检索结果里，"
                              "来源无法点开核对，可能是模型自带的知识）")
            demoted += 1
            continue

        bad_labels = _media_only_evidence(ev)
        if bad_labels:
            it["verdict"] = "半对"
            it["fix"] = ("补一手来源（官网/年报/标准/论文）后再定论"
                         if _no_change_fix(it.get("fix")) else it.get("fix"))
            it["evidence"] = (ev.rstrip("。") +
                              f"。（已降级：依据援引的 {'、'.join(bad_labels[:2])} "
                              "均非一手来源，属媒体转述，支撑不了确证）")
            demoted += 1
            continue

        if not tier:
            # 一条外部来源都没有。若依据全是「正文自己这么说」，
            # 这一条根本没被核过 —— 只能退回「未核」。
            if any(w in ev for w in _SELF_PROOF):
                it["verdict"] = "未核"
                it["fix"] = ("该查外部出处后再定论（当前依据只是正文自述，"
                             "用正文证明正文不构成核验）")
                it["evidence"] = (ev.rstrip("。") + "。（已降级为未核："
                                  "依据仅为正文自述，没有任何外部出处）")
                demoted += 1
            else:
                it["verdict"] = "半对"
                it["fix"] = ("补权威出处后再定论"
                             if _no_change_fix(it.get("fix")) else it.get("fix"))
                it["evidence"] = (ev.rstrip("。") + "。（已降级：判定确证但未落到任何外部来源）")
                demoted += 1
            continue
        if "权威" not in tier:
            it["verdict"] = "半对"
            it["fix"] = ("补权威出处后再定论"
                         if _no_change_fix(it.get("fix")) else it.get("fix"))
            it["evidence"] = (ev.rstrip("。") +
                              "。（已降级：所列来源均非权威，仅能说明「有多处相同说法」，不能算确证）")
            demoted += 1
    return demoted


def stage_trace(doc: dict, triage: dict, verify: dict) -> dict:
    """对「未核」的条目做一次外部检索，并把证据回灌给模型重判。

    三条自我约束：
      - 只查「未核」的，能判的不重复花钱；
      - 结果质量不足时宁可保持「未核」，不许硬升；
      - 检索失败不影响主流程，report 里如实写「未溯源」。
    """
    items = verify.get("items") or []
    todo = [it for it in items if it.get("verdict") == "未核"]
    if not todo:
        return {"items": items, "traced": 0, "reason": "没有未核项"}
    if not search.is_configured():
        return {"items": items, "traced": 0, "reason": f"溯源未启用（{search.describe()}）"}

    cfg = search.config()
    claims = triage.get("claims") or []
    pack = []
    for it in todo[: cfg["max_items"]]:
        n = it.get("n")
        quote = ""
        if isinstance(n, int) and 1 <= n <= len(claims):
            quote = str(claims[n - 1].get("quote", "")).strip()
        if not quote:
            continue
        try:
            ev = search.evidence_for(quote)
        except search.SearchError as exc:
            ev = {"query": quote, "authoritative": [], "relevant": [], "weak": [],
                  "total": 0, "error": str(exc)}
        pack.append({"n": n, "quote": quote, **ev})

    if not pack:
        return {"items": items, "traced": 0, "reason": "没有可检索的条目"}

    # 二次检索：前一批全是自媒体/门户时，按官方域名再查一遍。
    # 目的不是提高召回，而是把「到处都在说」升级成「一手来源这么说」。
    pending = [p for p in pack
               if not (p.get("authoritative") or [])
               and ((p.get("relevant") or []) or (p.get("weak") or []))]
    second = 0
    if pending:
        hints = _authority_hints(pending)
        for p in pending:
            sites = hints.get(p["n"]) or []
            if not sites:
                continue
            seen = {h["url"] for h in p.get("authoritative") or []}
            p["second_pass"] = []
            for site in sites:
                try:
                    more = search.evidence_for(f"{p['quote']} site:{site}")
                except search.SearchError:
                    continue
                # 白名单不可能穷举所有一手站点。模型既然指名了这个官方域名，
                # site: 又真在这个域名下搜到内容，就按一手来源算 —— 否则阿猫阿狗的
                # 机构官网永远进不来，只能永远判「半对」。
                cand = (more.get("authoritative") or []) + (more.get("relevant") or [])
                for h in cand:
                    if h["url"] in seen:
                        continue
                    host = search.host_of(h.get("url", ""))
                    forced = bool(site) and site.lower().strip(".") in host
                    if forced or h in (more.get("authoritative") or []):
                        if forced:
                            h["why"] = f"官方域名直检（{site}）"
                        p["authoritative"].append(h)
                        seen.add(h["url"])
                        second += 1
                p["second_pass"].append(site)

    blocks = []
    for p in pack:
        blocks.append(f"\n### 第 {p['n']} 项：{p['quote']}\n查询词：{p['query']}")
        if p.get("error"):
            blocks.append(f"（检索失败：{p['error']}）")
            continue
        auth = p.get("authoritative") or []
        rel = p.get("relevant") or []
        if not auth and not rel and not p.get("weak"):
            blocks.append("（无检索结果）")
            continue
        for h in auth:
            domain = search.domain_of(h["url"]) or h["site"]
            body = (h["summary"] or h["snippet"])[:300]
            blocks.append(f"- 【权威源】{h['title']}（{domain}）{h['url']}\n  {body}")
        for h in rel:
            domain = search.domain_of(h["url"]) or h["site"]
            body = (h["summary"] or h["snippet"])[:240]
            blocks.append(f"- 【相关但非权威】{h['title']}（{domain}）{h['url']}\n  {body}")
        for h in (p.get("weak") or []):
            blocks.append(f"- 【仅供参考】{h['title']}（{search.domain_of(h['url'])}）"
                          f"{h['url']}\n  {h['snippet'][:160]}")

    try:
        redo = llm.chat_json(
            [{"role": "system", "content": SYSTEM},
             {"role": "user", "content": TRACE_PROMPT + "\n".join(blocks)}],
            max_tokens=2600,
        )
    except llm.LLMError:
        redo = {"items": []}

    updates = {u.get("n"): u for u in (redo.get("items") or []) if isinstance(u, dict)}
    by_n = {p["n"]: p for p in pack}
    traced = 0
    for it in items:
        n = it.get("n")
        if n in updates:
            u = updates[n]
            old = it.get("verdict", "未核")
            new = str(u.get("verdict") or old).strip()
            if new and new != old:
                it["verdict"] = new
                traced += 1
                if new == "确证":
                    it["fix"] = "无需修改"     # 判确证了还留「应查…」会自相矛盾
            if u.get("evidence"):
                it["evidence"] = str(u["evidence"])
            if u.get("source"):
                it["source"] = str(u["source"])
        if n in by_n:
            # 出处列要跟「依据里实际引用的那个源」对得上。
            # v1.7 实况：依据写「据 sec.gov（NVIDIA 提交的文件）」，出处列却显示
            # blogs.nvidia.com —— 检索池里权威结果超过 3 条，sec.gov 被前 3 条挤掉了。
            ev_lower = str(it.get("evidence") or "").lower()
            auth_pool = by_n[n].get("authoritative") or []
            pool = auth_pool + (by_n[n].get("relevant") or [])

            def _rank(h, _auth=auth_pool):
                host = search.host_of(h.get("url", ""))
                bare = host[4:] if host.startswith("www.") else host
                # 依据里写「据 sec.gov」，URL 主机是 www.sec.gov —— 去掉 www 再比
                cited = 0 if (bare and bare in ev_lower) else 1
                return (cited, 0 if h in _auth else 1)

            picks = sorted(pool, key=_rank)[:3]
            # 依据里点名的来源若只在「仅供参考」桶，也得露个脸 ——
            # 否则报告写「据 sec.gov」，出处列却是 nvidia 博客，读者对不上。
            weak_pool = by_n[n].get("weak") or []
            for h in weak_pool:
                host = search.host_of(h.get("url", ""))
                bare = host[4:] if host.startswith("www.") else host
                if bare and bare in ev_lower and len(picks) < 4:
                    picks.insert(0, {**h, "_weak_cited": True})
                    break
            it["sources"] = [
                {"title": h["title"], "url": h["url"],
                 "site": search.domain_of(h["url"]) or h["site"],
                 "tier": ("仅供参考" if h.get("_weak_cited")
                          else ("权威" if h in auth_pool else "相关"))}
                for h in picks[:4]
            ]

            # 依据里写的出处，检索结果里到底有没有？
            # v1.9 实况：依据写「据 sec.gov（NVIDIA 冲突矿产报告）」，出处列却只有
            # blogs.nvidia.com —— 检索池里根本没有 sec.gov。这意味着那条引用是模型
            # 自己带进来的，读者无从核对。low 分桶里有的也算「检索到」。
            seen_hosts = {search.host_of(h.get("url", ""))
                          for h in (auth_pool + (by_n[n].get("relevant") or [])
                                    + (by_n[n].get("weak") or []))}
            missing = []
            for dom in _DOMAIN_RE.findall(ev_lower):
                if not _looks_like_domain(dom):
                    continue
                if any(dom == sh or sh.endswith("." + dom) or dom.endswith("." + sh)
                       for sh in seen_hosts if sh):
                    continue
                if dom not in missing:
                    missing.append(dom)
            if missing:
                it["_cites_missing"] = missing[:3]
    return {"items": items, "traced": traced, "demoted": 0, "pack": pack,
            "queried": len(pack), "second_pass": second}


def stage_compute(doc: dict, triage: dict) -> dict:
    """让模型给出可算的表达式，再由受限求值器实算——这是档 2 与档 1 的分界。"""
    claims = triage.get("claims") or []
    lines = "\n".join(f"- {c.get('quote','')}" for c in claims)
    req = (
        "第三步：挑出可以用算术验证的说法，给出可执行表达式。\n"
        f"正文：\n{_clip(_body(doc), 10000)}\n\n"
        f"候选说法：\n{lines}\n\n"
        "返回 JSON：{\"items\": [{\"claim\": \"被验的说法\", \"expr\": \"表达式\", "
        "\"symbols\": {\"变量\": 数值}, \"expected\": \"原文对结果的声称值\"}]}\n"
        "硬规则（不满足就不要输出这一条，宁可返回空数组）：\n"
        "- 表达式必须至少用到一个变量（symbols 里的），变量名要能说明含义，不能用数字当变量名；\n"
        "- symbols 的值必须直接来自原文，**不得引入原文没有的外部数据**（如自己查来的面积、份额）；\n"
        "- 必须给出 expected：原文对结果的明确声称。若原文根本没给这个值，就不要列这一条；\n"
        "- 变量名避开 Python 关键字（global、class、lambda 等），否则会被语法解析拒绝；\n"
        "- 若原文没有可对照的声称值，返回 {\"items\": []}，不要凑数。\n"
        "格式规则（否则会被拒绝执行）：\n"
        "- 只能是单个算术式，可用 + - * / ** 与 sqrt log log2 log10 exp abs min max round sin cos；\n"
        "- 变量只能来自 symbols，值必须是从文中读到的数字；\n"
        "- 不要写单位、不要写中文、不要写赋值、不要写函数定义；\n"
        "- 若没有可用算术验证的说法，返回 {\"items\": []}。\n"
        f"最多 {MAX_CALCS} 条。"
    )
    raw, _ = _json_with_retry(
        [{"role": "system", "content": SYSTEM}, {"role": "user", "content": req}],
        2000,
        "上一次输出被截断。这次只给最多 3 条算式，expected 不超过 20 字。只输出完整 JSON。",
    )

    records = []
    for it in (raw.get("items") or [])[:MAX_CALCS]:
        expr = str(it.get("expr", "")).strip()
        syms = it.get("symbols") or {}
        rec = {
            "claim": _trim(it.get("claim", ""), 90),
            "expr": _trim(expr, 160),
            "symbols": syms,
            "expected": _trim(it.get("expected", ""), 40),
            "value": None,
            "status": "未实算",
            "note": "",
        }
        if not expr:
            rec["note"] = "模型未给出表达式"
            records.append(rec)
            continue
        try:
            rec["value"] = evaluate(expr, syms)
        except UnsafeExpression as exc:
            rec["note"] = f"表达式被安全规则拒绝：{exc}"
            records.append(rec)
            continue
        except EvalError as exc:
            rec["note"] = f"计算失败：{exc}"
            records.append(rec)
            continue
        except Exception as exc:  # noqa: BLE001
            rec["note"] = f"计算异常：{type(exc).__name__}"
            records.append(rec)
            continue

        # 能算 ≠ 在验证。三条必要条件缺一，就只能算「仅供参考」。
        gaps = []
        ok_var, why_var = has_variable(expr, syms)
        if not ok_var:
            gaps.append(why_var)
        elif not has_operation(expr):
            gaps.append("表达式只有单个变量、没有运算，等于把原值抄一遍")
        ok_claim, why_claim = _claim_usable(rec["expected"], _body(doc))
        if not ok_claim:
            gaps.append(why_claim)
        if gaps:
            rec["status"] = "仅供参考"
            rec["note"] = "；".join(gaps) + _symbols_note(syms, doc["text"])
            records.append(rec)
            continue

        # 关键：声称值解析不出来时，绝不能默认「相符」。
        # v1.0 就栽在这里 —— 实算 100000、声称 5 万次，因为「5万」解析失败而判成「已实算」。
        want = _as_number(rec["expected"])
        # 原文说「约」「大概」时，1% 的尺子就太细了 —— 那本来就是个粗略说法
        loose = bool(re.search(r"约|大概|左右|差不多|将近|逾", rec["expected"]))
        tol = 0.15 if loose else 0.01
        if want is None:
            rec["status"] = "待比对"
            rec["note"] = (f"实算得 {rec['value']:.6g}，但声称值"
                           f"「{_trim(rec['expected'], 40)}」无法自动解析为数字，请人工对照")
        elif abs(rec["value"] - want) > max(abs(want) * tol, 1e-9):
            rec["status"] = "不符"
            rec["note"] = (f"实算 {rec['value']:.6g} 与声称「{rec['expected']}」"
                           f"相差超过 {int(tol*100)}%")
        else:
            rec["status"] = "已实算"
        records.append(rec)
    return {"items": records}


def stage_commentary(doc: dict, triage: dict, verify: dict, calc: dict) -> str:
    """只写评述，且不含数字——数字一律由模板从实算记录注入。"""
    verdicts = "\n".join(
        f"- {i.get('verdict','')}：{_trim(i.get('evidence',''), 80)}"
        for i in (verify.get("items") or [])
    )
    out = llm.chat(
        [
            {"role": "system", "content": SYSTEM},
            {
                "role": "user",
                "content": (
                    "第四步：写两段评述，共不超过 220 字。\n"
                    f"体裁：{triage.get('genre','')}（{triage.get('genre_reason','')}）\n"
                    f"核验结果：\n{verdicts}\n\n"
                    "要求：\n"
                    "1) 第一段写「它讲对了什么」——必须有，具体到某处讲对了；\n"
                    "2) 第二段写读者该带走的一句判断，或最该警惕的一处；\n"
                    "3) 两段都不许出现阿拉伯数字（数字由报告表格另外给出），"
                    "不许写「本文」「综上」这类空话。\n"
                    "直接输出两段文字，不要小标题。"
                ),
            },
        ],
        max_tokens=900,
    )
    return out.strip()


_DIGIT = re.compile(r"\d")


def _pasted_doc(url: str, text: str, title: str) -> dict:
    """用户直接粘贴正文时的 doc。反爬挡不住人，这是最后的兜底入口。

    注意：粘贴模式下作者序无从核对，必须在报告里如实标注，不能假装核过。
    """
    text = re.sub(r"\n{3,}", "\n\n", text.strip())
    guess_title = title.strip() or text.split("\n", 1)[0][:80]
    return {
        "url": url or "(手动粘贴)",
        "kind": "pasted",
        "title": guess_title,
        "authors": [],
        "published": "",
        "text": text,
        "n_chars": len(text),
        "notes": ["手动粘贴正文：作者序与发布信息均未从原始页面取得，相关核对只能标「未核」"],
    }


def _nonempty(items, keys) -> list:
    """丢掉全空的条目。模型常给「结构对、内容空」的对象，直接渲染会得到空表格 ——
    空表比不出现更糟，读者会以为内容丢了。"""
    out = []
    for it in items or []:
        if isinstance(it, dict):
            if any(str(it.get(k, "")).strip() for k in keys):
                out.append(it)
        elif str(it).strip():
            out.append(it)
    return out


def _cell(s: str, n: int = 120) -> str:
    """表格单元格：先截断、再把竖线转义，避免单元格里的 | 拆行。"""
    return _trim(s, n).replace("|", "\\|")


_CN_NUM = ["一", "二", "三", "四", "五", "六", "七", "八", "九"]

# 来源类型的展示名。内部枚举名（video_upload 之类）不许出现在报告里。
KIND_LABEL = {
    "wechat": "微信公众号",
    "wechat_channel": "微信视频号（经元宝总结中转）",
    "arxiv": "arXiv 论文",
    "generic": "网页",
    "pasted": "粘贴正文",
    "video_upload": "上传的音视频",
}


def render_report(url: str, doc: dict, triage: dict, verify: dict, calc: dict, commentary: str) -> str:
    """确定性拼装。所有数字都从这里注入，不经模型之手。"""
    KIND = KIND_LABEL
    L: list[str] = []
    L.append(f"# 读解：{doc['title'] or url}\n")
    L.append(f"- 来源：{url or '（无链接，仅上传音视频）'}")
    L.append(
        f"- 类型：{KIND.get(doc['kind'], doc['kind'])}　体裁判定：{triage.get('genre','未判定')}"
        f"（{triage.get('genre_reason','')}）"
    )
    if doc.get("layer"):
        L.append(f"- 来源层级：{doc['layer']}")
    if doc.get("transcript") and url:
        # 元信息来自链接、正文来自上传的音视频 —— 这种组合是支持的（视频号场景就靠它做
        # 「元宝总结 vs 原声」），但必须写出来；否则报告头是文章标题、正文是转录，
        # 读起来像两个来源混在一起（v2.13）。
        L.append("- 来源构成：标题 / 作者 / 发布时间来自上述链接；正文来自上传音视频的转录")
    if doc["authors"]:
        L.append(
            f"- 作者（按页面顺序）：{'、'.join(doc['authors'][:12])}"
            + ("（等）" if len(doc["authors"]) > 12 else "")
        )
    if doc["published"]:
        label = "元宝整理日期（非发布时间）" if doc["kind"] == "wechat_channel" else "发布信息"
        L.append(f"- {label}：{doc['published']}")
    L.append(f"- 正文长度：{doc['n_chars']} 字（送模型 {min(doc['n_chars'], MAX_CHARS)} 字）")
    for n in (doc.get("notes") or []):
        L.append(f"- 抓取说明：{n}")
    L.append("")

    sec = 0

    def head(title: str) -> None:
        nonlocal sec
        sec += 1
        idx = _CN_NUM[sec - 1] if sec <= len(_CN_NUM) else str(sec)
        L.append(f"## {idx}、{title}\n")

    head("作者与主体核对")
    L.append(triage.get("author_note") or "未核。这一项最容易被做手脚，也最不容易被读者去查。")
    L.append("")
    if doc["kind"] in ("wechat_channel", "video_upload"):
        if triage.get("who_speaks"):
            L.append(f"- 谁在说话：{triage['who_speaks']}")
        for r in _nonempty(triage.get("title_claims"), ("claim",)):
            if isinstance(r, dict):
                claim = r.get("claim", "")
                st = r.get("status", "未核")
                note = r.get("note", "")
                L.append(f"- 身份/头衔：{claim} —— {st}" + (f"（{note}）" if note else ""))
            else:
                L.append(f"- 身份/头衔：{r} —— 未核")
        L.append("")

    fixes = [f for f in (doc.get("transcript_fixes") or []) if isinstance(f, dict)]
    vinfo = doc.get("voice") or {}
    if vinfo:
        head("语音画像")
        L.append(
            "这一段判断「这是什么话、谁在说」，用来标定转录该信几分。"
            "**下面分「实测」与「推断」两栏**——实测来自波形与字符统计，推断是猜的，"
            "两者不能混着看。\n"
        )
        L.append("| 项目 | 结论 |")
        L.append("|---|---|")
        L.append(f"| 语种 | {_cell(vinfo.get('lang','无法判断'),40)} |")
        L.append(f"| 方言 | {_cell(vinfo.get('dialect','未判断'),60)} |")
        L.append(f"| 说话方式 | {_cell(vinfo.get('style','无法判断'),40)} |")
        L.append("")
        if vinfo.get("facts"):
            L.append("**实测**（从波形与字符算出来的）：\n")
            for f in vinfo["facts"]:
                if f:
                    L.append(f"- {f}")
            L.append("")
        if vinfo.get("guesses"):
            L.append("**推断**（不一定对，参考着看）：\n")
            for g in vinfo["guesses"]:
                if g:
                    L.append(f"- {g}")
            L.append("")
        if vinfo.get("risks"):
            L.append("**对转录的影响**：\n")
            for r in vinfo["risks"]:
                if r:
                    L.append(f"- {r}")
            L.append("")

    if fixes:
        head("转录校对（已自动修正）")
        L.append("ASR 有错字，以下是送入读解前做的修正，**下方核验所依据的文本是修正后的**。"
                 "本节列全部改动；「风险」一列标出改错的后果与是否需要回听，"
                 "带「·回听」标记的清单见下一节。\n")
        L.append("| # | 原文 | 改为 | 依据 | 风险 |")
        L.append("|---|---|---|---|---|")
        for i, f in enumerate(fixes[:12], 1):
            kind, advice = _fix_risk(f)
            mark = kind + ("·回听" if advice != "可接受" else "")
            L.append(f"| {i} | {_cell(f.get('wrong',''),40)} | {_cell(f.get('right',''),40)} "
                     f"| {_cell(f.get('why',''),60)} | {mark} |")
        if len(fixes) > 12:
            L.append(f"\n> 另有 {len(fixes) - 12} 处未列出。")
        L.append("")

    # ── 需要回听的地方 ────────────────────────────────────────
    # 两节的分工：上一节说「我改了什么」，这一节说「你该亲自听哪几处」。
    # 只筛两类：改动涉及数字/专名（改错会让后续核验建立在错前提上），
    # 以及模型提出疑问但依据不够硬、没有改动的。
    hot = []
    for f in fixes:
        kind, advice = _fix_risk(f)
        if advice != "可接受":
            hot.append((f, kind, advice))

    risks = _drop_self_duplicates(
        _nonempty(triage.get("transcript_risks"), ("as_is", "should_be", "why")))
    risks.sort(key=_risk_rank)

    if hot or risks:
        head("需要回听的地方")
        L.append("这一节不是再列一遍错字，而是挑出**改错代价最大**的几处，"
                 "和**模型拿不准、没有动**的几处。其余改动可以直接采用。\n")
        if hot:
            L.append("**这几处若改错，后果最重**——涉及数字、专名或术语，"
                     "而依据又没有硬到可以直接采用：\n")
            L.append("| 原文 | 改为 | 类别 | 建议 |")
            L.append("|---|---|---|---|")
            for f, kind, advice in hot[:8]:
                L.append(f"| {_cell(f.get('wrong',''),40)} | {_cell(f.get('right',''),40)} "
                         f"| {kind} | {advice} |")
            if len(hot) > 8:
                L.append(f"\n> 另有 {len(hot) - 8} 处同类改动未列出。")
            L.append("")
        if risks:
            L.append("**模型存疑但未采纳**——下面几条是它提出了疑问、依据又不够硬的，"
                     "原声没有改动，留给你判断：\n")
            if len(risks) > 8:
                _extra_risks = len(risks) - 8
                risks = risks[:8]
            else:
                _extra_risks = 0

            def _filled(row, key):
                v = str(row.get(key, "")).strip() if isinstance(row, dict) else ""
                return bool(v) and v != "—"

            structured = sum(1 for r in risks
                             if _filled(r, "should_be") and _filled(r, "why"))
            if structured * 2 < len(risks):
                # 模型把「原文／应为／依据」挤进同一句，硬排表格会有半列空白
                for i, r in enumerate(risks, 1):
                    text = str(r.get("as_is", "")) if isinstance(r, dict) else str(r)
                    L.append(f"{i}. {text}")
            else:
                L.append("| # | 原文这么写 | 应该是 | 依据 |")
                L.append("|---|---|---|---|")
                for i, r in enumerate(risks, 1):
                    if isinstance(r, dict):
                        L.append(
                            f"| {i} | {_cell(r.get('as_is',''),60)} "
                            f"| {_cell(r.get('should_be',''),60)} "
                            f"| {_cell(r.get('why',''),120)} |"
                        )
                    else:
                        L.append(f"| {i} | {_cell(str(r),120)} | — | — |")
            if _extra_risks:
                L.append(f"\n> 另有 {_extra_risks} 条未列出。")
            L.append("")

    checks = _nonempty(triage.get("summary_check"), ("point", "evidence"))
    if checks and doc.get("transcript") and doc.get("summary_text"):
        head("元宝总结 vs 原声")
        L.append(
            "有了原声转录，元宝的总结就可以被检验了。下表逐条对：总结说的，原声里有没有。\n"
        )
        L.append("| # | 元宝总结的要点 | 原声里 | 判定 | 原声证据 |")
        L.append("|---|---|---|---|---|")
        for i, c in enumerate(checks[:8], 1):
            if not isinstance(c, dict):
                continue
            L.append(
                f"| {i} | {_cell(c.get('point',''),50)} | {_cell(c.get('in_transcript',''),10)} "
                f"| {_cell(c.get('verdict',''),10)} | {_cell(c.get('evidence',''),90)} |"
            )
        L.append("")
        L.append(
            "> 「歪曲」指总结与原声方向相反；「省略」指原声有而总结没提；"
            "「添加」指总结有而原声找不到对应。判定由模型给出，证据列可直接回原声核。"
        )
        L.append("")

    head("逐条核验")
    items = verify.get("items") or []
    if items:
        L.append("| # | 判定 | 依据 | 出处 | 应改为 |")
        L.append("|---|---|---|---|---|")
        by_n = {i.get("n"): i for i in items}
        for idx, c in enumerate(triage.get("claims") or [], start=1):
            it = by_n.get(idx, {})
            srcs = it.get("sources") or []
            if srcs:
                seen_doms = []
                for x in srcs:
                    d = search.domain_of(str(x.get("url", ""))) or str(x.get("site", ""))
                    if d and d not in seen_doms:
                        seen_doms.append(d)
                src_cell = "、".join(seen_doms[:2])
                if len(srcs) > 2:
                    src_cell += f" 等（共 {len(srcs)} 条）"
                elif len(seen_doms) > 2:
                    src_cell += " 等"
                if all(x.get("tier") != "权威" for x in srcs):
                    src_cell += "（均非权威）"
            elif it.get("source"):
                src_cell = search.domain_of(str(it["source"]))
            else:
                src_cell = "—"
            L.append(
                f"| {idx} | {it.get('verdict','未核')} | {_cell(it.get('evidence','—'),160)} "
                f"| {_cell(src_cell, 56)} | {_cell(it.get('fix','—'),80)} |"
            )
        L.append("")
        tr = verify.get("_trace") or {}
        if tr.get("queried"):
            L.append(
                f"> 已对 {tr['queried']} 项做过外部检索，其中 {tr.get('traced', 0)} 项判定被更新。"
                + (f" 其中一轮结果全为自媒体或门户，已按官方域名补检，"
                   f"新增权威结果 {tr['second']} 条。" if tr.get("second") else "")
                + "「出处」只列高权威或高相关结果（authority ≥ 3 或 rerank ≥ 1.5）；"
                "低质量结果不计入，仍为「未核」表示检索不足以定论。"
            )
            L.append("")
        L.append("> 对照原文：")
        for idx, c in enumerate(triage.get("claims") or [], start=1):
            L.append(f"> {idx}. {c.get('quote','')}")
    else:
        L.append("未提取到可核项。")
    L.append("")

    head("实算（由受限求值器执行）")
    recs = calc.get("items") or []
    if recs:
        L.append("| 被验说法 | 表达式 | 原文声称 | 实算 | 判定 |")
        L.append("|---|---|---|---|---|")
        for r in recs:
            sym = "、".join(f"{k}={v}" for k, v in (r["symbols"] or {}).items())
            show = f"`{r['expr']}`" + (f"（{_cell(sym, 40)}）" if sym else "")
            val = f"{r['value']:.6g}" if r["value"] is not None else "—"
            want = _cell(r.get("expected", "") or "—", 40)
            status = r["status"] + (f"（{r['note']}）" if r["note"] else "")
            L.append(
                f"| {_cell(r['claim'],80)} | {show} | {want} | {val} "
                f"| {_cell(status,140)} |"
            )
        L.append("")
        L.append(
            "> 表达式在服务端以白名单 AST 求值器执行，不执行任意代码。"
            "判定含义：已实算＝算得出且与原文声称相符；不符＝两者差超过 1%；"
            "待比对＝算得出、但声称值是中文表述无法自动解析，需人工对照；"
            "仅供参考＝算式无运算、全为常量或原文未给声称值（这类只是复述，不构成验证）；"
            "未实算＝被安全规则拒绝或算不出来。\n"
            "> 边界：算式与符号取值由模型给出，服务端只保证算术本身执行正确，"
            "不保证算式表达的换算关系与原文语义一致。若判「不符」而原文看似没错，"
            "先检查算式是否把「每秒」当成了「每分钟」这类关系错位。"
        )
    else:
        L.append(
            "本篇文章未提出可用算术独立验证的说法（或模型未给出可执行表达式）——"
            "这不等于文中数字都对，只说明这一轮没算成。"
        )
    if recs and not any(r["status"] in ("已实算", "不符") for r in recs):
        L.append("")
        L.append(
            "> 注意：本轮没有形成可判定的实算（无「已实算」「不符」，"
            "只有「待比对」「仅供参考」「未实算」）。"
            "这意味着文中数字未被独立验证，不代表它们都对。"
        )
    L.append("")

    head("评述")
    if _DIGIT.search(commentary or ""):
        L.append("> 注意：以下评述含数字，但该数字未经本服务实算，请以上表为准。\n")
    L.append(commentary or "（未生成）")
    L.append("")

    L.append("---\n")
    tail = (
        "本报告由读解 SOP 自动生成。核验依据来自模型对原文与公开来源的比对，"
        "不能替代读者本人对原始出处的核对；标「未核」「未实算」之处请勿当作已确认。"
    )
    if doc.get("transcript"):
        tail += (
            " 本次已使用原声转录（ASR），但转录本身可能有错字，"
            "涉及引用与数字请回原声复核。"
        )
    elif doc["kind"] in ("wechat_channel", "video_upload"):
        tail += (
            " 本对象是元宝对视频的总结而非视频原话：总结本身若有遗漏或转述偏差，"
            "本服务无法发现。上传原视频即可升为原声读解。"
        )
    L.append(tail)
    return "\n".join(L)


# ---------------------------------------------------------------- 简约版
BRIEF_MAX = 5


def _fmt_num(v) -> str:
    """给读者看的数值去掉无意义的尾零：90.0 → 90，90.5 保留。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if f == int(f) and abs(f) < 1e15:
        return str(int(f))
    return f"{f:g}"


def _count_verdicts(verify: dict, calc: dict) -> dict:
    """统计判定分布。简约版里的每个数字都出自这里，不经模型。"""
    items = [it for it in (verify.get("items") or [])
             if isinstance(it, dict) and it.get("verdict")]
    c = {"n": len(items), "确证": 0, "不实": 0, "半对": 0, "未核": 0, "其他": 0,
         "降级": 0}
    for it in items:
        v = str(it["verdict"]).strip()
        if v in ("确证", "不实", "半对", "未核"):
            c[v] += 1
            # 「确证被降级成半对」与「说法本身只有一半对」不是一回事：
            # 前者的意思是「引文与原文一致，只是拿不出外部出处」。两者混算，
            # 一份引用逐字无误的转录也会被判「问题不少（2/3）」。v2.19 分开。
            if v == "半对" and "已降级" in str(it.get("evidence") or ""):
                c["降级"] += 1
        else:
            c["其他"] += 1     # 模型偶尔自造判定（如「存疑」）。旧实现直接丢掉，
                               # 于是「可核 5 条」而分项只加得 4 条 —— 数字自己打架。
                               # 现在单列一档，分项之和恒等于 n。
    recs = [r for r in (calc.get("items") or []) if isinstance(r, dict)]
    c["已实算"] = sum(1 for r in recs if r.get("status") == "已实算")
    c["不符"] = sum(1 for r in recs if r.get("status") == "不符")
    return c


def _brief_verdict_line(c: dict) -> str:
    """一句话结论：三档 + 两条限定，全部由计数算出。"""
    n = c["n"]
    if n == 0:
        return ("**没列到可核项** —— 可能通篇是观点，也可能正文没抓全。"
                "先确认原文完整，再决定这份报告值不值得看。")
    bad = c["不实"] + c["半对"] - c.get("降级", 0)
    other = c.get("其他", 0)
    if bad == 0 and other == 0:
        # 有降级项或未核项时不说「都站得住」—— 它们没被证伪，但也没被证实。
        head = (f"**{n} 条里没有发现明显错误**" if (c.get("降级") or c["未核"])
                else f"**核到的 {n} 条都站得住**")
    elif bad / n <= 0.3:
        head = (f"**大体可核，{bad} 处需要改**" if bad
                else f"**大体可核，但有 {other} 条判定不明**")
    else:
        head = f"**问题不少（{bad}/{n}），建议对照原文再判断**"
    tails = []
    if c["不符"]:
        tails.append(f"另有 {c['不符']} 处实算与原文声称不符")
    if other:
        tails.append(f"{other} 条判定不明")
    if c.get("降级"):
        tails.append(f"{c['降级']} 条引文与原文一致，但找不到外部出处")
    if c["未核"]:
        tails.append(f"{c['未核']} 条查不到出处（是没核，不是没问题）")
    return head + ("；" + "；".join(tails) if tails else "") + "。"


def render_brief(url: str, doc: dict, triage: dict, verify: dict,
                 calc: dict, commentary: str) -> str:
    """简约版：一屏之内回答「这篇能不能信、哪几处要改」。

    详细版（render_report）是取证记录，简约版是给读者的结论。
    分层的理由见 docs/ARCHITECTURE.md 决策 8 —— 一次全给，等于把「判断」
    和「判断的依据」堆在同一屏，读者多半只看前者，而前者恰恰最需要被核对。
    """
    c = _count_verdicts(verify, calc)
    claims = triage.get("claims") or []

    def _q(it: dict) -> str:
        """条目引文。verify 的 items 是模型产出，而提示词里压根没有 quote 字段 ——
        真实引文在 triage.claims 里，必须按 n 回填。否则简约版会渲染出
        「-  —— 半对」这种只有判定、没有对象的残行。v2.19 修。"""
        q = str(it.get("quote") or "").strip()
        if q:
            return q
        n = it.get("n")
        if isinstance(n, int) and 1 <= n <= len(claims):
            return str(claims[n - 1].get("quote") or "").strip()
        return ""

    L = [f"# {doc.get('title') or url}\n"]

    L.append(_brief_verdict_line(c))
    L.append("")

    stat = [f"可核 {c['n']} 条"]
    if c["确证"]:
        stat.append(f"有出处 {c['确证']}")
    hard = c["不实"] + c["半对"] - c.get("降级", 0)
    if hard:
        stat.append(f"有问题 {hard}")
    if c.get("降级"):
        stat.append(f"出处不足 {c['降级']}")
    if c["未核"]:
        stat.append(f"查不到 {c['未核']}")
    if c.get("其他"):
        stat.append(f"判定不明 {c['其他']}")
    if c["已实算"]:
        stat.append(f"实算通过 {c['已实算']}")
    if c["不符"]:
        stat.append(f"实算不符 {c['不符']}")
    src = KIND_LABEL.get(doc.get("kind"), "")
    if doc.get("authors"):
        src += "·" + "、".join(doc["authors"][:2])
    L.append("· " + " · ".join(stat) + (f"　｜　来源：{src}" if src else ""))
    L.append("")

    oks = [it for it in (verify.get("items") or [])
           if isinstance(it, dict) and it.get("verdict") == "确证"][:3]
    if oks:
        L.append("## 站得住的部分")
        for it in oks:
            q = _trim(_q(it), 60)
            ev = _trim(it.get("evidence", ""), 46)
            L.append(f"- {q}" + (f"（依据：{ev}）" if ev else ""))
        L.append("")

    bads = [it for it in (verify.get("items") or [])
            if isinstance(it, dict) and it.get("verdict") in ("不实", "半对")]
    L.append("## 需要改的地方")
    if bads:
        for it in bads[:BRIEF_MAX]:
            q = _trim(_q(it), 60)
            fix = _trim(it.get("fix", ""), 40)
            raw_ev = str(it.get("evidence") or "")
            # 降级项的 evidence 把原因写在末尾，按 46 字截断正好把原因截掉 ——
            # 读者只看到「原文逐字一致」却判半对，比不写依据更费解。
            ev = ("引文与原文一致，但找不到外部出处" if "已降级" in raw_ev
                  else _trim(raw_ev, 46))
            line = f"- {q} —— **{it.get('verdict')}**"
            if fix:
                line += f"：{fix}"
            if ev:
                line += f"（依据：{ev}）"
            L.append(line)
        if len(bads) > BRIEF_MAX:
            L.append(f"- 另有 {len(bads) - BRIEF_MAX} 处，见详细版")
    else:
        L.append("没有发现需要改的。**这不等于本文没问题**，只等于本次核到的部分站得住。")
    L.append("")

    wrong = [r for r in (calc.get("items") or [])
             if isinstance(r, dict) and r.get("status") == "不符"][:3]
    if wrong:
        L.append("## 算不上的地方")
        for r in wrong:
            L.append(f"- {_trim(r.get('claim', ''), 50)}：算得 {_fmt_num(r.get('value'))}，"
                     f"原文声称 {r.get('expected')}")
        L.append("")

    first = (commentary or "").strip().split("。")[0].strip()
    if first and not _DIGIT.search(first):
        L.append("## 评述（模型写的，未核）")
        L.append(first + "。")
        L.append("")

    L.append("---")
    L.append(f"本报告由 AI 依据所给正文生成，**未经人工复核**。"
             f"完整核验（{c['n']} 条，含依据与出处）见「详细版」。")
    L.append("")
    L.append("> 建议先自己读一遍原文，再对照这份核验 —— 读解能省时间，不能替你判断。")
    return "\n".join(L)


PROOFREAD_PROMPT = (
    "下面是语音识别的原始转录。请**只修以下几类错误**，不得改写、删减、补充任何内容：\n"
    "- 同音字错（如「拖住」应为「托住」、「及紫外」应为「极紫外」）；\n"
    "- 中文数字被误识成阿拉伯数字（如「几十万」被写成「几10万」）；\n"
    "- 断句造成的粘连或错断（如「。做」应为「的」）。\n"
    "**不要**润色、不要调整语序、不要补全省略、不要改口语为书面语。\n"
    '返回 JSON：{"text": "修正后的全文", '
    '"fixes": [{"wrong": "几10万", "right": "几十万", "why": "十被识成 10"}]}\n'
    "没有可修的就原样返回 text，fixes 给空数组。"
)


def _fix_rules() -> list[tuple[str, str]]:
    """ASR_FIX="几10万=几十万;及紫外=极紫外" 形式的硬替换，零成本。"""
    raw = os.environ.get("ASR_FIX", "")
    out = []
    for pair in raw.split(";"):
        if "=" not in pair:
            continue
        a, b = pair.split("=", 1)
        a, b = a.strip(), b.strip()
        if a and a != b:
            out.append((a, b))
    return out


def _apply_fix_rules(text: str) -> tuple[str, list[dict]]:
    applied = []
    for wrong, right in _fix_rules():
        if wrong in text:
            n = text.count(wrong)
            text = text.replace(wrong, right)
            applied.append({"wrong": wrong, "right": right, "why": f"规则替换（{n} 处）"})
    return text, applied


def _proofread_enabled() -> bool:
    return os.environ.get("ASR_PROOFREAD", "").strip().lower() in ("1", "on", "true", "yes")


def _proofread(text: str) -> tuple[str, list[dict]]:
    """让模型只修错字。失败就原样返回，绝不因为校对失败丢转录。"""
    if not text.strip():
        return text, []
    try:
        out = llm.chat_json(
            [{"role": "system", "content": SYSTEM},
             {"role": "user", "content": PROOFREAD_PROMPT + "\n\n原文：\n" + text}],
            max_tokens=max(800, min(4000, len(text) * 2)),
        )
    except llm.LLMError:
        return text, []
    clean = str(out.get("text") or "").strip()
    fixes = [f for f in (out.get("fixes") or []) if isinstance(f, dict)]
    # 长度急剧缩水说明它在改写，宁可不采信
    if not clean or len(clean) < len(text) * 0.9:
        return text, []
    return clean, fixes


def _promote_to_l0(doc: dict, asr_out: dict, prep: dict, media_name: str) -> dict:
    """转写成功后把来源从 L1 升到 L0。

    意义在于把「核总结」换成「核原话」——只有原话在手，
    「总结有没有歪曲」才有得判。元宝总结不丢，降为对照材料。
    """
    d = dict(doc)
    summary = d.get("summary_text", "")

    head = []
    if d.get("title"):
        head.append(f"标题：{d['title']}")
    if d.get("authors"):
        head.append(f"视频号主体：{'、'.join(d['authors'])}")
    if prep.get("duration"):
        head.append(f"时长：{int(prep['duration'])} 秒（{asr.stamp(prep['duration'])}）")
    if media_name:
        head.append(f"上传文件：{media_name}")

    body = ["视频原声转录（ASR，可能有识别错误）：", asr_out["text"]]
    if summary:
        body += ["", "元宝生成的视频总结（供对照，非原话）：", summary]

    d["text"] = "\n".join(head + [""] + body)
    d["transcript"] = asr_out["text"]
    d["transcript_segments"] = asr_out.get("segments") or []
    d["transcript_fixes"] = asr_out.get("fixes") or []
    d["summary_text"] = summary
    d["asr"] = {
        "provider": asr_out.get("provider", ""),
        "chars": len(asr_out["text"]),
        "parts": asr_out.get("parts", 1),
    }
    if prep.get("duration"):
        d["duration_sec"] = int(prep["duration"])
    d["layer"] = "L0 原声转录（ASR，可能有识别错误）"
    d["n_chars"] = len(d["text"])

    notes = [n for n in (d.get("notes") or []) if "L1 二手" not in n]
    notes.insert(
        0,
        f"已转录原声 {len(asr_out['text'])} 字（后端 {asr_out.get('provider','?')}，"
        f"{asr_out.get('parts',1)} 段）",
    )
    notes.append("ASR 会有错字，同音术语尤甚；引用前请对照原声")
    if summary:
        notes.append(f"元宝总结 {len(summary)} 字，已作为对照一并送入读解")
    d["notes"] = notes
    return d


def analyze(
    url: str = "",
    pasted_text: str = "",
    pasted_title: str = "",
    media_path: str = "",
    media_name: str = "",
) -> Generator[dict, None, None]:
    """流式执行。任一阶段失败都显式暴露，不静默降级。

    来源优先级：上传音视频（转写 → L0） > 粘贴正文 > 链接抓取。
    """
    pasted = (pasted_text or "").strip()
    has_media = bool(media_path)

    yield {"stage": "extract", "status": "run",
           "text": "整理输入…" if has_media else "抓取正文…"}

    doc: dict
    if has_media and not url and len(pasted) < 200:
        doc = {
            "url": "", "kind": "video_upload",
            "title": pasted_title or media_name or "上传的音视频",
            "authors": [], "published": "", "text": "", "n_chars": 0,
            "notes": ["仅上传了音视频，未提供链接：主体与发布时间无从核对"],
        }
    elif len(pasted) >= 200:
        doc = _pasted_doc(url, pasted, pasted_title)
    elif url:
        try:
            doc = extract.fetch(url)
        except extract.FetchError as exc:
            if not has_media:
                yield {"stage": "extract", "status": "fail",
                       "text": f"抓取失败：{exc}（可改用「粘贴正文」或上传音视频）"}
                return
            doc = {
                "url": url, "kind": "video_upload", "title": pasted_title or media_name,
                "authors": [], "published": "", "text": "", "n_chars": 0,
                "notes": [f"页面抓取失败（{exc}），只用了上传的音视频"],
            }
    else:
        yield {"stage": "extract", "status": "fail",
               "text": "没有可用输入：链接、正文、音视频至少给一样"}
        return

    yield {
        "stage": "extract",
        "status": "ok",
        "text": f"已取到「{_trim(doc['title'] or url, 50)}」"
                + (f"{doc['n_chars']} 字" if doc.get("n_chars") else "（待转写）"),
        "data": {
            "title": doc["title"],
            "authors": doc["authors"][:12],
            "published": doc["published"],
            "n_chars": doc["n_chars"],
            "notes": doc["notes"],
        },
    }

    if has_media:
        if not asr.is_configured():
            yield {"stage": "transcribe", "status": "fail",
                   "text": f"无法转写上传的音视频：{asr.describe()}"}
            return
        yield {"stage": "transcribe", "status": "run",
               "text": f"转写中（{asr.describe()}）…"}
        workdir = tempfile.mkdtemp(prefix="deepread_")
        voice_acoustic = None
        try:
            spec = asr.audio_spec()
            prep = media.prepare(media_path, workdir,
                                 max_sec=spec["max_sec"], fmt=spec["fmt"])
            if len(prep["parts"]) > 1:
                yield {"stage": "transcribe", "status": "info",
                       "text": f"音频 {int(prep['duration'])}s，按 {prep['seg_sec']}s "
                               f"切成 {len(prep['parts'])} 片（{spec['why']}）"}
            out = asr.transcribe_parts(prep["parts"], prep["seg_sec"])
            # 语音画像的声学部分要在 workdir 删掉之前算完
            try:
                voice_acoustic = voice._acoustic_many(prep["parts"])
                voice_duration = float(prep.get("duration") or 0)
            except Exception:      # noqa: BLE001  画像失败绝不影响主流程
                voice_acoustic, voice_duration = None, 0.0
        except (media.MediaError, asr.ASRError) as exc:
            yield {"stage": "transcribe", "status": "fail", "text": f"转写失败：{exc}"}
            return
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

        # 转录后处理：先把确定性规则替换掉，再（可选）让模型校对错字。
        # DeepSeek ASR 不收 prompt 参数，所以「给术语提示」这条路走不通，
        # 只能放在转录之后修 —— 这是唯一有效的做法。
        raw_text = out["text"]
        fixed, applied = _apply_fix_rules(raw_text)
        notes = []
        if applied:
            notes.append(f"规则替换 {len(applied)} 条")
        if _proofread_enabled():
            yield {"stage": "transcribe", "status": "info", "text": "校对转录中的错字…"}
            fixed, fixes = _proofread(fixed)
            if fixes:
                applied.extend(fixes)
                notes.append(f"校对修正 {len(fixes)} 处")
        if fixed != raw_text:
            out["text"] = fixed

        # 语音画像：说什么话、谁在说、说得清不清楚 —— 决定转录该信几分
        try:
            doc["voice"] = voice.combine(
                voice_acoustic, out["text"],
                float(locals().get("voice_duration") or 0))
        except Exception:          # noqa: BLE001
            doc["voice"] = None
            out["fixes"] = applied
            out["raw_chars"] = len(raw_text)

        doc = _promote_to_l0(doc, out, prep, media_name)
        yield {
            "stage": "transcribe", "status": "ok",
            "text": f"转录 {len(out['text'])} 字"
                    + (f"（{'、'.join(notes)}）" if notes else "")
                    + "，来源升为 L0（原声）",
            "data": {"chars": len(out["text"]), "provider": out.get("provider"),
                     "layer": doc["layer"], "duration": doc.get("duration_sec"),
                     "fixes": len(applied)},
        }

    if not llm.is_configured():
        yield {"stage": "extract", "status": "fail",
               "text": "服务端未配置 LLM_API_KEY，无法继续（不降级为假报告）"}
        return

    try:
        yield {"stage": "triage", "status": "run", "text": "判体裁、核作者序、列可核项…"}
        triage = stage_triage(doc)
        if triage.get("_retried"):
            yield {"stage": "triage", "status": "info",
                   "text": "首次输出被截断，已自动精简要求重试一次（清单可能少几条）"}
        yield {"stage": "triage", "status": "ok",
               "text": f"体裁：{triage.get('genre','未判定')}；提出 {len(triage.get('claims') or [])} 项待核",
               "data": {"genre": triage.get("genre"), "author_note": triage.get("author_note"),
                        "claims": triage.get("claims"),
                        "summary_check": triage.get("summary_check")}}

        yield {"stage": "verify", "status": "run", "text": "逐条核验…"}
        verify = stage_verify(doc, triage)
        if verify.get("_retried"):
            yield {"stage": "verify", "status": "info",
                   "text": "核验输出被截断，已自动精简要求重试一次"}
        ok_n = sum(1 for i in (verify.get("items") or []) if i.get("verdict") == "确证")
        yield {"stage": "verify", "status": "ok",
               "text": f"核验完成（确证 {ok_n} 项）", "data": {"items": verify.get("items")}}

        if search.is_configured():
            yield {"stage": "trace", "status": "run", "text": "为未核项检索外部出处…"}
            trace = stage_trace(doc, triage, verify)
            verify = {"items": trace["items"],
                      "_trace": {"queried": trace.get("queried", 0),
                                "traced": trace["traced"],
                                "second": trace.get("second_pass", 0)}}
            queried = trace.get("queried", 0)
            if queried:
                yield {"stage": "trace", "status": "ok",
                       "text": f"检索 {queried} 项，判定更新 {trace['traced']} 项",
                       "data": {"queried": queried, "traced": trace["traced"]}}
            else:
                yield {"stage": "trace", "status": "warn",
                       "text": f"未检索：{trace.get('reason', '无未核项')}"}

        # 闸：判「确证」必须拿得出外部证据。放在 trace 之后、成稿之前，
        # 因为「确证」既可能在核验阶段产生，也可能在 trace 重判时产生。
        demoted = enforce_evidence(verify.get("items") or [])
        if demoted:
            yield {"stage": "verify", "status": "info",
                   "text": (f"{demoted} 项「确证」被降级：拿不出外部出处，"
                            "（用正文证明正文、或来源全非权威）")}

        yield {"stage": "compute", "status": "run", "text": "生成表达式并实算…"}
        calc = stage_compute(doc, triage)
        real = sum(1 for r in calc["items"] if r["status"] == "已实算")
        bad = sum(1 for r in calc["items"] if r["status"] == "不符")
        ref = sum(1 for r in calc["items"] if r["status"] == "仅供参考")
        pend = sum(1 for r in calc["items"] if r["status"] == "待比对")
        skip = len(calc["items"]) - real - bad - ref - pend
        bits = []
        if real:
            bits.append(f"实算 {real}")
        if bad:
            bits.append(f"不符 {bad}")
        if pend:
            bits.append(f"待比对 {pend}")
        if ref:
            bits.append(f"仅供参考 {ref}")
        if skip:
            bits.append(f"未实算 {skip}")
        yield {"stage": "compute", "status": "ok" if (real or bad) else "warn",
               "text": "、".join(bits) + " 条" if bits else "无可算项",
               "data": {"items": calc["items"]}}

        yield {"stage": "render", "status": "run", "text": "拼装报告…"}
        commentary = stage_commentary(doc, triage, verify, calc)
        md = render_report(url, doc, triage, verify, calc, commentary)
        # 必须显式收尾：前端只在 status=run/ok/warn/fail 时改标签，
        # 少了这一条「成稿」会永远停在「…」（v2.12 实测发现）。
        yield {"stage": "render", "status": "ok", "text": "报告已生成"}
        brief = render_brief(url, doc, triage, verify, calc, commentary)
        yield {"stage": "done", "status": "ok", "text": f"完成（{len(md)} 字符）",
               "data": {"md": md, "brief": brief}}
    except llm.LLMNotConfigured as exc:
        yield {"stage": "triage", "status": "fail", "text": str(exc)}
    except llm.LLMError as exc:
        yield {"stage": "triage", "status": "fail", "text": f"模型调用失败：{exc}"}
    except Exception as exc:  # noqa: BLE001
        yield {"stage": "triage", "status": "fail", "text": f"内部错误：{type(exc).__name__}: {exc}"}
