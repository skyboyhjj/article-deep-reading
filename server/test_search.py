"""Search 溯源链路的夹具。

测四件事：
  1. 只对「未核」项检索，已判定的不重复花钱；
  2. 高质量结果让判定从「未核」升级为「确证」，并带出处；
  3. 低质量结果不得单独支撑「确证」；
  4. 未配置时跳过且如实说明，不假装查过。

运行：python3 test_search.py
"""

import sys

import llm
import pipeline
import search

TRIAGE = {
    "genre": "视频口播",
    "genre_reason": "第一人称讲述",
    "author_note": "未取到作者信息。",
    "who_speaks": "",
    "claims": [
        {"quote": "英伟达从来没有自己的芯片制造厂", "why": "事实", "kind": "事实"},
        {"quote": "极紫外光刻机只有阿斯麦一家能造", "why": "事实", "kind": "事实"},
        {"quote": "EDA 全球 3/4 市场在三家手里", "why": "数字", "kind": "数字"},
        {"quote": "锡液滴每秒 5 万滴", "why": "数字", "kind": "数字"},
    ],
    "transcript_risks": [],
    "title_claims": [],
    "summary_check": [],
}

# 第 1 项判「确证」（不该再检索）、第 2-4 项「未核」（该检索）
VERIFY = {"items": [
    {"n": 1, "verdict": "确证", "evidence": "正文自身明确表述", "fix": "无需修改"},
    {"n": 2, "verdict": "未核", "evidence": "无出处", "fix": "查 ASML 官网"},
    {"n": 3, "verdict": "未核", "evidence": "无出处", "fix": "查 Gartner"},
    {"n": 4, "verdict": "未核", "evidence": "无出处", "fix": "查厂商资料"},
]}

HITS = {
    "英伟达从来没有自己的芯片制造厂": [
        {"title": "NVIDIA 10-K", "url": "https://nvidia.com/10k", "site": "nvidia.com",
         "snippet": "we do not own fabrication facilities", "summary": "NVIDIA 为 fabless 公司，制造外包。",
         "rerank": 2.6, "authority": 4, "date": "", "why": "高权威源（authority 4）"},
    ],
    "极紫外光刻机只有阿斯麦一家能造": [
        {"title": "ASML EUV 说明", "url": "https://asml.com/euv", "site": "asml.com",
         "snippet": "ASML is the only supplier of EUV", "summary": "ASML 是 EUV 光刻机整机唯一供应商。",
         "rerank": 2.4, "authority": 4, "date": "", "why": "高权威源（authority 4）"},
    ],
    "EDA 全球 3/4 市场在三家手里": [
        {"title": "某博客转载", "url": "https://blog.example.com/eda", "site": "blog.example.com",
         "snippet": "三家占七成", "summary": "内容为转载，无统计口径。",
         "rerank": 0.9, "authority": 1, "date": "", "why": "质量不足（rerank 0.9 / authority 1）"},
    ],
    "锡液滴每秒 5 万滴": [],
}


def install_fakes():
    llm.is_configured = lambda: True
    search.is_configured = lambda: True
    search.describe = lambda: "deepseek /tools/search（夹具）"

    def fake_evidence(query, limit=3):
        if "site:gartner.com" in query:      # 二次检索：官方域名命中权威源
            hits = [{"title": "Gartner EDA 市场份额", "url": "https://gartner.com/eda",
                     "site": "gartner.com", "snippet": "三家合计约 74%",
                     "summary": "Gartner 报告：Synopsys/Cadence/Siemens EDA 合计约七成四。",
                     "rerank": 2.5, "authority": 4, "date": "", "why": "权威源"}]
        else:
            hits = HITS.get(query, [])
        auth = [h for h in hits if h["authority"] >= 3]
        rel = [h for h in hits if h["authority"] < 3 and h["rerank"] >= 1.5]
        weak = [h for h in hits if h["authority"] < 3 and h["rerank"] < 1.5]
        return {"query": query, "authoritative": auth[:limit], "relevant": rel[:limit],
                "weak": weak[:2], "total": len(hits)}

    search.evidence_for = fake_evidence
    pipeline._authority_hints = lambda pending: {3: ["gartner.com"]}

    # 模型「重判」：有可作证据的就升为确证，否则保持未核
    def fake_json(messages, **kw):
        text = str(messages)
        if "第一步" in text:
            return TRIAGE
        if "只依据这些结果" in text:
            items = []
            if "极紫外光刻机只有阿斯麦一家能造" in text:
                items.append({"n": 2, "verdict": "确证",
                              "evidence": "据 ASML EUV 说明（asml.com）",
                              "source": "https://asml.com/euv"})
            if "EDA 全球 3/4 市场在三家手里" in text:
                if "gartner" in text.lower() and "【权威源】" in text:
                    items.append({"n": 3, "verdict": "确证",
                                  "evidence": "据 Gartner EDA 市场份额报告（gartner.com）",
                                  "source": "https://gartner.com/eda"})
                else:
                    items.append({"n": 3, "verdict": "未核",
                                  "evidence": "检索只得到低质量转载，不足以定论",
                                  "source": ""})
            if "锡液滴每秒 5 万滴" in text:
                items.append({"n": 4, "verdict": "未核", "evidence": "无检索结果", "source": ""})
            return {"items": items}
        return {"items": []}

    llm.chat_json = fake_json
    llm.chat = lambda messages, **kw: "评述无数字。"
    pipeline.llm = llm
    pipeline.search = search


def main() -> int:
    install_fakes()
    bad = 0

    def chk(name, cond, extra=""):
        nonlocal bad
        print(("[ok] " if cond else "[FAIL] ") + name + (f"  {extra}" if extra and not cond else ""))
        if not cond:
            bad += 1

    doc = {"url": "https://yb.tencent.com/wx/ct/f/X", "kind": "wechat_channel",
           "layer": "L1 二手", "title": "夹具", "authors": ["某人"], "published": "",
           "text": "正文" * 100, "n_chars": 200, "notes": []}
    triage = pipeline.stage_triage(doc)
    out = pipeline.stage_trace(doc, triage, {"items": [dict(x) for x in VERIFY["items"]]})

    items = {i["n"]: i for i in out["items"]}
    chk("只对未核项检索（3 项）", out.get("queried") == 3, f"queried={out.get('queried')}")
    chk("确证项未被重新判定", items[1]["verdict"] == "确证")
    chk("有权威证据的升级为确证", items[2]["verdict"] == "确证")
    chk("升级项带出处", any("asml" in s["site"] for s in items[2].get("sources", [])))
    chk("首轮低质量不直接升级，二次检索后升级", items[3]["verdict"] == "确证")
    chk("二次检索有计数", (out.get("second_pass") or 0) > 0, f"second={out.get('second_pass')}")
    chk("二次检索来源为官网", any("gartner" in s["site"] for s in items[3].get("sources", [])))
    chk("无结果保持未核", items[4]["verdict"] == "未核")
    chk("判定更新计数为 2（首轮 + 二次）", out["traced"] == 2, f"traced={out['traced']}")

    # 渲染：核验表要有「出处」列与溯源说明
    md = pipeline.render_report(
        doc["url"], doc, triage,
        {"items": out["items"], "_trace": {"queried": out["queried"], "traced": out["traced"]}},
        {"items": []}, "评述无数字。")
    chk("表头含「出处」", "| 出处 |" in md)
    chk("出处列有域名", "asml.com" in md)
    chk("有溯源说明", "已对 3 项做过外部检索" in md)

    # 未配置时：跳过 + 如实说明
    search.is_configured = lambda: False
    out2 = pipeline.stage_trace(doc, triage, {"items": [dict(x) for x in VERIFY["items"]]})
    chk("未配置时跳过", out2.get("queried") is None and out2["traced"] == 0)
    chk("未配置时说明原因", "未启用" in out2.get("reason", ""))

    # 出处全为非权威来源时必须标出来
    items_rel = [{"n": 1, "verdict": "确证", "evidence": "据某博客", "fix": "无需修改",
                  "sources": [{"title": "blog", "url": "https://blog.example.com/x",
                               "site": "blog.example.com", "tier": "相关"}]}]
    md_rel = pipeline.render_report("u", doc, {"claims": [{"quote": "q"}], "_trace": {}},
                                    {"items": items_rel}, {"items": []}, "评述。")
    chk("出处全非权威要标记", "（均非权威）" in md_rel)

    # 模型把三列挤成一句话时，降级为列表而不是空表
    triage_merged = dict(TRIAGE)
    triage_merged["transcript_risks"] = [
        {"as_is": "「几10万」→ 应为「几十万」→ 依据：ASR 误识", "should_be": "—", "why": "—"}
    ] * 3
    # v1.4 实况：mp.weixin.qq.com 被搜索接口打了 authority=3，于是公众号成了「权威源」
    from search import quality as _q
    def _q2(url, auth):
        return _q({"url": url, "authority": auth, "rerank": 2.0,
                   "title": "", "snippet": "", "summary": ""})[0]
    chk("公众号永不算权威（即使 auth=3）", _q2("https://mp.weixin.qq.com/s/x", 3) == "relevant")
    chk("新浪财经永不算权威（即使 auth=4）", _q2("https://finance.sina.cn/a", 4) == "relevant")
    chk("快科技永不算权威", _q2("https://m.mydrivers.com/x", 4) == "relevant")
    chk("ACM 无分数也算权威", _q2("https://dl.acm.org/doi/1", 2) == "authoritative")
    chk("机构官网无分数也算权威", _q2("https://www.zeiss.com/x", 0) == "authoritative")
    # v1.6 实况：名单外的媒体拿高分就被当成权威 —— 现在一律不认
    chk("三立新闻网 auth=4 仍非权威", _q2("https://rwdnews.sanlih.com.tw/x", 4) == "relevant")
    chk("日经中文网 auth=4 仍非权威", _q2("https://zh.cn.nikkei.com/x", 4) == "relevant")
    chk("艾瑞咨询 auth=4 仍非权威", _q2("https://news.iresearch.cn/x", 4) == "relevant")
    chk("电子工程专辑 auth=3 仍非权威", _q2("https://wap.seccw.com/x", 3) == "relevant")
    chk("zenodo 预印本算一手", _q2("https://zenodo.org/x", 1) == "authoritative")

    # v1.7 实况：依据写 sec.gov，出处列却显示 blogs.nvidia.com（被前 3 条挤掉）
    tr_items = [{"n": 1, "verdict": "未核", "quote": "英伟达没有制造厂",
                 "evidence": "", "fix": ""}]
    claims = [{"quote": "英伟达没有制造厂"}]
    pack = [{"n": 1, "quote": "英伟达没有制造厂",
             "authoritative": [
                 {"title": "NVIDIA blog", "url": "https://blogs.nvidia.com/a",
                  "site": "blogs", "summary": "x", "snippet": "x",
                  "authority": 3, "rerank": 2.0},
                 {"title": "NVIDIA blog2", "url": "https://la.blogs.nvidia.com/b",
                  "site": "blogs", "summary": "x", "snippet": "x",
                  "authority": 3, "rerank": 2.0},
                 {"title": "SEC filing", "url": "https://www.sec.gov/c",
                  "site": "SEC", "summary": "fabless", "snippet": "fabless",
                  "authority": 3, "rerank": 2.0},
                 {"title": "ASML 页", "url": "https://www.asml.com/d",
                  "site": "ASML", "summary": "x", "snippet": "x",
                  "authority": 3, "rerank": 2.0}],
             "relevant": [], "weak": []}]
    search.is_configured = lambda: True     # 前面测「未配置」时被改成 False 了
    real_chat = llm.chat_json
    llm.chat_json = lambda *a, **k: {"items": [
        {"n": 1, "verdict": "确证",
         "evidence": "据 sec.gov（NVIDIA 提交的文件）：fabless manufacturing strategy…"}]}
    real_ev = search.evidence_for
    search.evidence_for = lambda q, *a, **k: {
        "query": q, "authoritative": pack[0]["authoritative"],
        "relevant": [], "weak": [], "total": 4}
    real_hints = pipeline._authority_hints
    pipeline._authority_hints = lambda pending: {}
    try:
        out = pipeline.stage_trace(doc, {"claims": claims}, {"items": tr_items})
        srcs = out["items"][0]["sources"]
        chk("出处优先对齐依据（sec.gov 排第一）", "sec.gov" in srcs[0]["url"])
        chk("检索结果里有 sec.gov 不算假引用",
            not out["items"][0].get("_cites_missing"))

        # 反向：依据引用了一个检索结果里根本没有的域名
        llm.chat_json = lambda *a, **k: {"items": [
            {"n": 1, "verdict": "确证",
             "evidence": "据 fas.org（某智库报告）：NVIDIA 采用 fabless 策略…"}]}
        fresh = [{"n": 1, "verdict": "未核", "quote": "英伟达没有制造厂",
                  "evidence": "", "fix": ""}]
        out2 = pipeline.stage_trace(doc, {"claims": claims}, {"items": fresh})
        chk("假引用的来源会被标出",
            out2["items"][0].get("_cites_missing") == ["fas.org"])
        pipeline.enforce_evidence(out2["items"])
        chk("假引用降为半对", out2["items"][0]["verdict"] == "半对")
    finally:
        llm.chat_json = real_chat
        search.evidence_for = real_ev
        pipeline._authority_hints = real_hints

    md_mg = pipeline.render_report("u", doc, triage_merged, {"items": []},
                                   {"items": []}, "评述。")
    # 纯文本报告的小节标题是「最该留意的地方」（v2.33 起按有无音频分），
    # 这里只关心「存疑项有没有渲染出来」，不绑标题
    chk("字段合并时降级为列表", "模型存疑但未采纳" in md_mg
        and "1. " in md_mg.split("模型存疑但未采纳")[-1])
    chk("降级后内容不丢", "几10万" in md_mg)

    print("\n" + ("Search 链路通过" if bad == 0 else f"{bad} 项失败"))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
