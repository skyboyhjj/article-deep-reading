"""针对「自产报告质量审计」的回归，用两版真实报告里出现的算式当夹具。

v0.6 实测暴露的新绕过：模型把常量塞进 symbols、在 expected 里自述「原文未给予」，
从而同时满足「有变量」与「有声称值」两条形式约束，实质仍是复述。
本测试锁死四件事：
  1. 空对象不许渲染成空表格；
  2. 纯常量式（3/4、50000*60、0.1/357000000）判「仅供参考」；
  3. expected 自述「未给/未核」视为没有对照值；
  4. symbols 里原文找不到的数值要提示（可能是模型引入的外部数据）。
另测真有效算不被误杀。

运行：python3 test_audit.py
"""

import sys

import llm
import pipeline

TRIAGE = {
    "genre": "视频口播",
    "genre_reason": "第一人称连续讲述",
    "author_note": "未取到作者信息。",
    "who_speaks": "",
    "claims": [{"quote": "英伟达从来没有自己的芯片制造厂", "why": "事实", "kind": "事实"}],
    "transcript_risks": [{}, {}, {}, {}, {}, {}],     # v0.5 出现过：6 个空壳
    "title_claims": [{"claim": "", "status": ""}, {"claim": "", "status": ""}],
    "summary_check": [],
}

# v0.6 报告里实际出现的五条「算式」——表面各有花样，实质全是复述
CALC_V06 = {
    "items": [
        {"claim": "每秒5万滴", "expr": "50000 * 60",
         "symbols": {"50000": 50000, "60": 60},
         "expected": "每秒5万滴，即每分钟300万滴（原文未给分钟值，仅可核每秒量级）"},
        {"claim": "600瓦 vs 100瓦", "expr": "600 / 100",
         "symbols": {"600": 600, "100": 100},
         "expected": "工业量产功率是实验室的6倍"},
        {"claim": "德国国土起伏", "expr": "0.1 / 357000000",
         "symbols": {"0.1": 0.1, "357000000": 357000000},
         "expected": "相对起伏约2.8e-10（原文未给德国面积，需外部核验，未核）"},
        {"claim": "EDA 3/4", "expr": "3 / 4",
         "symbols": {"3": 3, "4": 4},
         "expected": "0.75，即75%市场份额"},
        {"claim": "20年", "expr": "20 * 12",
         "symbols": {"20": 20, "12": 12},
         "expected": "240个月（原文仅称20年，无对照值，未核）"},
    ]
}

# 真有效算：两个语义词变量 + 原文声称值
CALC_VALID = {
    "items": [
        {"claim": "进口占全球市场比例", "expr": "import_value / global_market",
         "symbols": {"import_value": 4000, "global_market": 6000},
         "expected": "0.67"},
        {"claim": "声称的差额", "expr": "high_power - low_power",
         "symbols": {"high_power": 600, "low_power": 100},
         "expected": "450"},
    ]
}

DOC_TEXT = ("英伟达从来没有自己的芯片制造厂。台积电手里的刻刀全世界只有阿斯麦一家能造。"
            "发光的光源来自美国，反射光线的十多面极紫外镜子来自德国蔡司。"
            "设计芯片用的EDA工业软件全球3/4的市场在三家欧美巨头手里。"
            "每秒钟能够喷出5万滴。功率大概100瓦上下，但工业量产的标准是600瓦。"
            "把镜子放大到整个德国国土那么大，地表最高凸起不能超过0.1mm。"
            "阿斯麦在破产边缘熬了整整20年才卖出第一台量产机。")


def install():
    llm.is_configured = lambda: True
    state = {"n": 0}

    def fake_json(messages, **kw):
        if "第一步" in str(messages):
            return TRIAGE
        state["n"] += 1
        return CALC_V06 if state["n"] == 1 else CALC_VALID

    llm.chat_json = fake_json
    llm.chat = lambda messages, **kw: "它讲对了产业分工这条主线。"
    pipeline.llm = llm


def main() -> int:
    install()
    bad = 0

    def chk(name, cond):
        nonlocal bad
        print(("[ok] " if cond else "[FAIL] ") + name)
        if not cond:
            bad += 1

    doc = {
        "url": "", "kind": "video_upload", "layer": "L0 原声转录（ASR，可能有识别错误）",
        "title": "testUpload", "authors": [], "published": "",
        "text": DOC_TEXT, "transcript": DOC_TEXT, "n_chars": len(DOC_TEXT),
        "notes": ["已转录原声 1752 字（后端 deepseek，4 段）"],
    }
    triage = pipeline.stage_triage(doc)

    # ---- 第一组：v0.6 的五条算式必须全部降级 ----
    calc = pipeline.stage_compute(doc, triage)
    st = [r["status"] for r in calc["items"]]
    chk("五条全部判「仅供参考」", st == ["仅供参考"] * 5)
    chk("外部数据有提示（357000000）", "357000000" in calc["items"][2]["note"])
    chk("声称值自述未核被判无效", "未核" in calc["items"][4]["note"]
        or "对照值" in calc["items"][4]["note"])

    # ---- 第二组：真有效算不能被误杀 ----
    calc2 = pipeline.stage_compute(doc, triage)
    st2 = [r["status"] for r in calc2["items"]]
    chk("有效算判「已实算」", st2[0] == "已实算")
    chk("差超 1% 判「不符」", st2[1] == "不符")

    # ---- 第三组：v1.0 实况回放（假算与假权威） ----
    # 原型：100000 vs 声称「每秒钟打5万次」，v1.0 判「已实算」（中文单位解析失败）
    chk("中文单位能解析", pipeline._as_number("每秒钟打5万次") == 50000.0)
    chk("分数能解析（约1/6）", abs(pipeline._as_number("还不够工业级别的亮（约1/6）") - 1/6) < 1e-9)
    chk("分数能解析（1/3）", abs(pipeline._as_number("一滴只有头发丝的1/3粗细") - 1/3) < 1e-9)
    chk("「翻倍」能解析", pipeline._as_number("成本就直接翻倍了") == 2.0)
    chk("「未明说」算没有对照值", not pipeline._claim_usable("原文未明说倍数，仅给两个数值")[0])
    # 原型：`eda_share` 单变量，v1.0 判「已实算」（无运算等于抄一遍）
    from safe_eval import has_operation
    chk("单变量不算算式", not has_operation("eda_share"))
    chk("真算式才算算式", has_operation("a/b"))

    # ---- 依据里全媒体、却判「确证」 ----
    ev_media = ("据三立新闻网（rwdnews.sanlih.com.tw）报道，辉达根本没有自己的晶片制造厂；"
                "日经中文网（zh.cn.nikkei.com）亦载…")
    chk("依据全媒体会被抓出来", bool(pipeline._media_only_evidence(ev_media)))
    chk("数字不被误认成域名",
        pipeline._media_only_evidence("正文称 13.5nm、0.1mm，均无出处") == [])
    chk("有一手来源则不拦",
        pipeline._media_only_evidence("据 dl.acm.org 论文…") == [])
    chk("完全不提域名则不拦",
        pipeline._media_only_evidence("据公开资料，此事属实") == [])
    # v1.8 实况：55 条转述偏差里大半是「「A」→ 需确认是否写作「A」而非「B」」的猜测
    guesses = [
        "「弯道超车」→ 需确认原文是否写作「弯道超车」而非「弯道超车」，成语。",
        "「台积电」→ 需确认原文是否写作「台积电」而非「台机电」，专有名词。",
        "「量产机」→ 需确认原文是否写作「量产机」而非「量产鸡」，术语。",
    ]
    chk("猜测型偏差被滤掉", pipeline._drop_self_duplicates(guesses) == [])
    real = [{"as_is": "及紫外", "should_be": "极紫外", "why": "同音，EUV 标准译名"},
            {"as_is": "拖住", "should_be": "托住", "why": "上下文讲不通"}]
    chk("有依据的偏差保留", len(pipeline._drop_self_duplicates(real)) == 2)
    chk("同字重复被滤掉",
        pipeline._drop_self_duplicates([{"as_is": "托住", "should_be": "托住", "why": "x"}]) == [])
    # v2.1 实况：原文只说「3.3% 和 2.1%」，模型却写「下降的1/3」当声称值
    body = "美国从1960年到2008年平均每年GDP增长3.3%，2008到2024平均只有2.1%"
    chk("模型自造的声称值会被拦住", not pipeline._claim_usable("下降的1/3", body)[0])
    chk("原文真有的数字放行", pipeline._claim_usable("3.3%", body)[0])
    chk("中文分数与阿拉伯分数等价",
        pipeline._claim_usable("三分之一", "下降了三分之一")[0])
    chk("「减了一半」能解析", pipeline._as_number("减了一半") == 0.5)

    # v2.1 实况：判「不实」却写着「应查…再判断是否成立」—— 自己都没查清
    doubt = [{"n": 1, "verdict": "不实", "quote": "组装厂",
              "fix": "应查ASML年报…再判断「组装厂」说法是否成立",
              "evidence": "据 asml.com 年报：制造活动均在洁净室完成",
              "sources": [{"url": "https://www.asml.com/x", "tier": "权威"}]}]
    pipeline.enforce_evidence(doubt)
    chk("判不实要有相反证据（存疑退回半对）", doubt[0]["verdict"] == "半对")
    proven = [{"n": 2, "verdict": "不实", "quote": "x", "fix": "应改为「整机集成商」",
               "evidence": "据 asml.com 年报原文「只是一家组装厂」不成立",
               "sources": [{"url": "https://www.asml.com/x", "tier": "权威"}]}]
    pipeline.enforce_evidence(proven)
    chk("有相反证据的不实保留", proven[0]["verdict"] == "不实")

    chk("「未直接给出」算没有声称值",
        not pipeline._claim_usable("原文未直接给出倍数，仅称约100瓦")[0])

    chk("中文单位不误报", pipeline._symbols_note({"d": 50000}, "每秒钟喷出5万滴") == "")
    chk("原文真没有的数仍要报",
        "找不到" in pipeline._symbols_note({"d": 50000}, "原文没提这个数"))

    # ---- v2.3：两节分工（校对列全部 / 回听只筛高风险） ----
    from pipeline import _fix_risk
    chk("规则替换算硬依据", _fix_risk({"wrong": "几10万", "right": "几十万",
                                      "why": "规则替换（2 处）"})[1] == "可接受")
    chk("纯同音无上下文要回听", _fix_risk({"wrong": "才拖住了", "right": "才托住了",
                                          "why": "同音字"})[1] == "术语/人名请回听")
    chk("上下文依据可省略回听", _fix_risk({"wrong": "你不再", "right": "当你不再",
                                          "why": "句首缺当，上下文为条件句"})[1] == "可接受")
    chk("数字类改错后果最重", _fix_risk({"wrong": "50000", "right": "5000",
                                        "why": "多打一个零"})[0] == "数字")

    fixes_demo = [
        {"wrong": "几10万", "right": "几十万", "why": "规则替换（2 处）"},
        {"wrong": "才拖住了这颗", "right": "才托住了这颗", "why": "同音字"},
    ]
    doc2 = dict(doc, transcript_fixes=fixes_demo)
    md2 = pipeline.render_report("", doc2, {"claims": [], "author_note": "未核",
                                           "transcript_risks": []},
                                 {"items": []}, {"items": []}, "评。")
    chk("校对表出现风险列", "| 依据 | 风险 |" in md2)
    chk("需回听的标 ·回听", "同音字·回听" in md2)
    chk("出「需要回听」节", "需要回听的地方" in md2)
    chk("只收高风险那条", "才拖住了这颗" in md2.split("需要回听的地方")[1])

    # 有模型存疑项时，并入同一节并标明未采纳
    doc3 = dict(doc, transcript_fixes=[])
    md3 = pipeline.render_report("", doc3,
                                 {"claims": [], "author_note": "未核",
                                  "transcript_risks": [
                                      {"as_is": "及紫外", "should_be": "极紫外",
                                       "why": "同音，EUV 标准译名"}]},
                                 {"items": []}, {"items": []}, "评。")
    chk("存疑项并入需回听节", "需要回听的地方" in md3 and "模型存疑但未采纳" in md3)
    chk("存疑项内容不丢", "及紫外" in md3)
    chk("不再出现旧节名", "疑似转述偏差" not in md3)

    # ---- 第四组：渲染层 ----
    md = pipeline.render_report("", doc, triage, {"items": []}, calc, "评述无数字。")
    # v2.3 起：两节分工改掉了 —— 「转录校对」列全部改动（带风险标记），
    # 「需要回听的地方」只筛高风险项。两者都空时不再出空节，也不再补兜底话。
    chk("无高风险项时不出「需要回听」空节", "需要回听的地方" not in md)
    chk("没有空表格行", "| 1 |  |" not in md)
    chk("上传音视频来源有占位", "（无链接，仅上传音视频）" in md)
    doc_pasted = dict(doc, kind="pasted")
    md_pasted = pipeline.render_report("", doc_pasted, triage, {"items": []}, calc, "评述无数字。")
    chk("粘贴正文来源不误写成上传音视频",
        "（无链接，粘贴正文）" in md_pasted and "仅上传音视频" not in md_pasted)
    chk("无有效实算有显式提示", "没有形成可判定的实算" in md)
    chk("表头含「原文声称」", "| 原文声称 |" in md)

    print("\n状态分布：", st)
    print("\n" + ("审计回归通过" if bad == 0 else f"{bad} 项失败"))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
