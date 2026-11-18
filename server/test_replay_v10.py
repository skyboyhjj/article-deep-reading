#!/usr/bin/env python3
"""v1.0 实况回放：把 testUpload 报告里的 4 条「实算」与 2 条「确证」原样喂回去，
看新的判定层给出什么结论。不联网、不调模型（llm 被打桩）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pipeline  # noqa: E402
import search  # noqa: E402

# 原文里得有这些数字：v2.1 起会核对「声称值是否真在原文里」，
# 夹具若不含这些词，会被判「模型自己算的」而落「仅供参考」
DOC = {
    "text": ("芯片产业科普口播（占位）。每秒钟打5万滴，成本直接翻倍，"
             "还不够工业级别的亮（约1/6），一滴只有头发丝的1/3粗细，"
             "良率从八成九成降到三到四成，正好2倍，实验室约100瓦、量产600瓦。"),
    "transcript": ("芯片产业科普口播（占位）。每秒钟打5万滴，成本直接翻倍，"
                   "还不够工业级别的亮（约1/6），一滴只有头发丝的1/3粗细，"
                   "良率从八成九成降到三到四成，正好2倍，实验室约100瓦、量产600瓦。"),
}

# ---- v1.0 报告里那 4 条，一字不改 ----
ITEMS = [
    {"claim": "每秒钟能够喷出5万滴锡液滴，每秒钟打5万次",
     "expr": "drops_per_second * shots_per_drop",
     "symbols": {"drops_per_second": 50000, "shots_per_drop": 2},
     "expected": "每秒钟打5万次（即每秒脉冲数等于液滴数，而非液滴数的两倍）"},
    {"claim": "实验室功率约100瓦，工业量产标准是600瓦",
     "expr": "industrial_power / lab_power",
     "symbols": {"industrial_power": 600, "lab_power": 100},
     "expected": "工业量产标准是实验室功率的6倍（原文未明说倍数，仅给两个数值）"},
    {"claim": "一道工序拆成4道，良品率从八成九成降到三成四成，成本直接翻倍",
     "expr": "old_yield_low / new_yield_high",
     "symbols": {"old_yield_low": 0.8, "new_yield_high": 0.4},
     "expected": "成本直接翻倍（良率比约2倍）"},
    {"claim": "EDA工业软件全球3/4市场在三家欧美巨头手里",
     "expr": "eda_share", "symbols": {"eda_share": 0.75},
     "expected": "全球3/4市场"},
]

# ---- v1.0 的两条「确证」与它们的来源 ----
TRACE_ITEMS = [
    {"n": 1, "verdict": "未核", "quote": "英伟达从来没有自己的芯片制造厂",
     "evidence": "据 mp.weixin.qq.com（权威源）：英伟达自身没有工厂…",
     "fix": "无需修改", "sources": []},
    {"n": 2, "verdict": "未核", "quote": "极紫外光刻机全世界只有荷兰阿斯麦一家能造",
     "evidence": "据 dl.acm.org / cacm.acm.org（权威源）：ASML Monopoly…",
     "fix": "无需修改", "sources": []},
]
UPDATES = {
    "items": [
        {"n": 1, "verdict": "确证", "evidence": TRACE_ITEMS[0]["evidence"]},
        {"n": 2, "verdict": "确证", "evidence": TRACE_ITEMS[1]["evidence"]},
    ]
}


class FakeLLM:
    """把 stage_compute / stage_trace 的两次调用换成固定返回。"""
    calls = 0

    @staticmethod
    def chat_json(*a, **k):
        FakeLLM.calls += 1
        return {"items": ITEMS} if FakeLLM.calls == 1 else UPDATES

    @staticmethod
    def chat(*a, **k):
        return "评述占位。"


pipeline.llm = FakeLLM

# 让 trace 的 pack 走真实搜索层算 tier（不打桩），但检索结果手工塞进去
FAKE_HITS = {
    1: {"authoritative": [], "relevant": [
        {"title": "英伟达没有工厂", "url": "https://mp.weixin.qq.com/s/x", "site": "公众号", "summary": "英伟达自身没有工厂", "snippet": "英伟达自身没有工厂"},
        {"title": "英伟达不产内存", "url": "https://finance.sina.cn/a", "site": "新浪财经", "summary": "英伟达不产内存芯片", "snippet": "英伟达不产内存芯片"},
    ]},
    2: {"authoritative": [
        {"title": "ASML Monopoly", "url": "https://dl.acm.org/doi/1", "site": "ACM", "summary": "以 ASML Monopoly 描述 EUV 光刻", "snippet": "以 ASML Monopoly 描述 EUV 光刻"},
        {"title": "CACM", "url": "https://cacm.acm.org/x", "site": "CACM", "summary": "2025 年出货 48 台 EUV", "snippet": "2025 年出货 48 台 EUV"},
    ], "relevant": []},
}


def fake_evidence(query, *a, **k):
    n = 1 if "英伟达" in query else 2
    return {"query": query, "total": 2, "weak": [],
            **FAKE_HITS.get(n, {"authoritative": [], "relevant": []})}


search.evidence_for = fake_evidence
search.is_configured = lambda: True
search.config = lambda: {"max_items": 10}
pipeline._authority_hints = lambda pending: {}


def main():
    calc = pipeline.stage_compute(DOC, {"claims": []})
    print("=" * 74)
    print("实算判定 · v1.0 原样数据 → v1.2 结论")
    print("=" * 74)
    for r in calc["items"]:
        print(f"\n  {r['claim'][:34]}")
        print(f"    expr      {r['expr']}")
        print(f"    实算值    {r['value']:.6g}")
        print(f"    原文声称  {r['expected'][:44]}")
        print(f"    → 判定    {r['status']}")
        if r.get("note"):
            print(f"      理由    {r['note'][:70]}")

    print("\n" + "=" * 74)
    print("出处判定 · 模型说「确证」，代码按域名复核 tier")
    print("=" * 74)
    # ---- v1.1 实况：用正文证明正文的一条「确证」 ----
    self_proof = [{
        "n": 1, "verdict": "确证", "quote": "英伟达从来没有自己的芯片制造厂",
        "evidence": ("正文自身表述「英伟达从来没有自己的芯片制造厂……」与说法一致；"
                     "英伟达为无晶圆厂（fabless）设计公司属公开常识性事实，"
                     "但严格依据仍来自正文自述。"),
        "fix": "无需修改", "sources": [],
    }]
    pipeline.enforce_evidence(self_proof)
    chk("正文自证的「确证」→ 降为未核（v1.1 曾判确证）", self_proof[0]["verdict"] == "未核")
    chk("降级后要求补外部出处", "外部出处" in self_proof[0]["fix"])

    claims = [{"quote": it["quote"]} for it in TRACE_ITEMS]
    out = pipeline.stage_trace(DOC, {"claims": claims}, {"items": [dict(i) for i in TRACE_ITEMS]})
    for it in out["items"]:
        tiers = [s.get("tier") for s in it.get("sources") or []]
        print(f"\n  #{it['n']} {it['quote'][:30]}")
        print(f"    来源      {[s.get('site') for s in it.get('sources') or []]}")
        print(f"    tier      {tiers}")
        print(f"    → 判定    {it['verdict']}")
    print(f"\n  降级 {out['demoted']} 项（模型判确证但无权威源）")


_failures = 0


def chk(name, cond):
    """演示模式与断言模式共用；失败累加到模块级 _failures。"""
    global _failures
    print(("[ok] " if cond else "[X ] ") + name)
    if not cond:
        _failures += 1


def test_all() -> int:
    """断言版：这些是 v1.0 出过错的实例，不能回退。"""
    global _failures
    _failures = 0

    calc = pipeline.stage_compute(DOC, {"claims": []})
    st = [r["status"] for r in calc["items"]]
    chk("100000 vs 声称 5 万 → 不符（v1.0 曾判已实算）", st[0] == "不符")
    chk("expected 自述「未明说」→ 仅供参考", st[1] == "仅供参考")
    chk("良率比 2 倍 vs 声称约 2 倍 → 已实算", st[2] == "已实算")
    chk("单变量 eda_share → 仅供参考", st[3] == "仅供参考")

    # ---- v1.1 实况：用正文证明正文的一条「确证」 ----
    self_proof = [{
        "n": 1, "verdict": "确证", "quote": "英伟达从来没有自己的芯片制造厂",
        "evidence": ("正文自身表述「英伟达从来没有自己的芯片制造厂……」与说法一致；"
                     "英伟达为无晶圆厂（fabless）设计公司属公开常识性事实，"
                     "但严格依据仍来自正文自述。"),
        "fix": "无需修改", "sources": [],
    }]
    pipeline.enforce_evidence(self_proof)
    chk("正文自证的「确证」→ 降为未核（v1.1 曾判确证）", self_proof[0]["verdict"] == "未核")
    chk("降级后要求补外部出处", "外部出处" in self_proof[0]["fix"])

    claims = [{"quote": it["quote"]} for it in TRACE_ITEMS]
    out = pipeline.stage_trace(DOC, {"claims": claims},
                               {"items": [dict(i) for i in TRACE_ITEMS]})
    pipeline.enforce_evidence(out["items"])
    v = {it["n"]: it["verdict"] for it in out["items"]}
    chk("公众号判确证 → 降为半对", v[1] == "半对")
    chk("ACM 判确证 → 保持确证", v[2] == "确证")
    # ---- 第五组：v1.4 实况，4 条中文声称值的自动比对 ----
    V14 = [
        {"claim": "良率三到四成，成本翻倍", "expr": "1 / yield_old",
         "symbols": {"yield_old": 0.35}, "expected": "成本就直接翻倍了（相对八成九成良品率）"},
        {"claim": "每秒5万滴", "expr": "drop_rate * 60",
         "symbols": {"drop_rate": 50000}, "expected": "每秒钟5万滴"},
        {"claim": "100瓦 vs 600瓦", "expr": "power_lab / power_industry",
         "symbols": {"power_lab": 100, "power_industry": 600},
         "expected": "亮了但还不够工业级别的亮（约1/6）"},
        {"claim": "头发丝 1/3", "expr": "hair_diameter / 3",
         "symbols": {"hair_diameter": 1}, "expected": "一滴只有头发丝的1/3粗细"},
    ]

    class F:
        @staticmethod
        def chat_json(*a, **k):
            return {"items": V14}

    pipeline.llm = F
    c14 = pipeline.stage_compute(DOC, {"claims": []})
    s14 = [r["status"] for r in c14["items"]]
    chk("良率 35% → 成本 2.86 倍 vs 声称翻倍 → 不符", s14[0] == "不符")
    chk("「约1/6」能自动比对 → 已实算", s14[2] == "已实算")
    chk("「1/3」能自动比对 → 已实算", s14[3] == "已实算")
    chk("声称值不再落「待比对」", "待比对" not in s14)

    # ---- 第六组：v1.6 实况，媒体当权威 ----
    v16 = [
        {"n": 1, "verdict": "确证", "quote": "英伟达没有制造厂",
         "evidence": ("据三立新闻网（rwdnews.sanlih.com.tw）报道…；"
                      "日经中文网（zh.cn.nikkei.com）亦载…"),
         "fix": "无需修改",
         "sources": [{"url": "https://rwdnews.sanlih.com.tw/x", "tier": "相关"},
                     {"url": "https://zh.cn.nikkei.com/x", "tier": "相关"}]},
        {"n": 2, "verdict": "确证", "quote": "EUV 只有 ASML 能造",
         "evidence": "据 iresearch（news.iresearch.cn）报道…；新浪财经转述…",
         "fix": "无需修改",
         "sources": [{"url": "https://zenodo.org/x", "tier": "权威"},
                     {"url": "https://news.iresearch.cn/x", "tier": "相关"}]},
        {"n": 3, "verdict": "确证", "quote": "ACM 支撑的一条",
         "evidence": "据 dl.acm.org（权威源）：ASML Monopoly…", "fix": "无需修改",
         "sources": [{"url": "https://dl.acm.org/doi/1", "tier": "权威"}]},
    ]
    d16 = pipeline.enforce_evidence(v16)
    v16v = {i["n"]: i["verdict"] for i in v16}
    chk("三立+日经当权威 → 降半对", v16v[1] == "半对")
    chk("艾瑞当权威 → 降半对", v16v[2] == "半对")
    chk("ACM 真权威 → 保持确证", v16v[3] == "确证")
    chk("降级计数为 2", d16 == 2)

    print("\n" + ("v1.0 回放回归通过" if _failures == 0 else f"{_failures} 项失败"))
    return 0 if _failures == 0 else 1


if __name__ == "__main__":
    if "--assert" in sys.argv:
        sys.exit(test_all())
    main()
