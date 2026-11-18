"""端到端夹具：用假模型替身跑完五阶段，验证数据流与"降级透明"。

不测模型质量（那是模型的事），只测三件事：
  1. 五个阶段都能产出事件，最后有 done 且带 md；
  2. 实算结果确实进了报告，且未经模型改写；
  3. 表达式被拒时，报告标「未实算」而非静默丢弃。

运行：python3 test_pipeline_e2e.py
"""

import sys

import llm
import pipeline

TRIAGE = {
    "genre": "个人思辨",
    "genre_reason": "通篇无数据出处",
    "author_note": "页面未给出作者信息，无法核对作者序。",
    "claims": [
        {"quote": "效率提升一百倍", "why": "数字", "kind": "数字"},
        {"quote": "这是已证明的定理", "why": "定性", "kind": "术语"},
    ],
}
VERIFY = {
    "items": [
        {"n": 1, "verdict": "不实", "evidence": "原文同段自述为「估算」", "fix": "改为估算"},
        {"n": 2, "verdict": "未核", "evidence": "未给出定理编号或出处", "fix": "标注为推测"},
    ]
}
CALC = {
    "items": [
        {"claim": "效率提升一百倍", "expr": "1000/10", "symbols": {}, "expected": "100"},
        {"claim": "越权尝试", "expr": "open('/etc/passwd')", "symbols": {}, "expected": ""},
    ]
}


def install_fakes():
    llm.is_configured = lambda: True
    seq = {"n": 0}

    def fake_json(messages, **kw):
        i = seq["n"]
        seq["n"] += 1
        return [TRIAGE, VERIFY, CALC][min(i, 2)]

    llm.chat_json = fake_json
    llm.chat = lambda messages, **kw: "它讲对了一处：把估算标成了估算。最该警惕的是把估算说成实测。"
    pipeline.llm = llm


def _report_header_checks() -> list[tuple[str, bool]]:
    """v2.13：混合来源必须显式；内部枚举名不许出现在报告里。

    起因：URL 框留着旧链接 + 上传视频时，报告头是文章标题/作者/时间，
    正文却是视频转录，读起来像「杂交体」。这种组合本身是支持的
    （视频号场景靠它做「元宝总结 vs 原声」），缺的是一行说明。
    """
    base = {
        "title": "测试标题", "authors": ["某号"], "published": "2026-11-13 17:48",
        "n_chars": 500, "text": "正文" * 100, "notes": [], "kind": "wechat",
    }
    url = "https://mp.weixin.qq.com/s/x"
    empty_calc = {"items": []}      # 这一组只看报告头部，实算节不需要

    mixed = dict(base, transcript="转录文本", layer="L0 原声转录")
    md_mixed = pipeline.render_report(url, mixed, TRIAGE, VERIFY, empty_calc, "评述")

    md_plain = pipeline.render_report(url, base, TRIAGE, VERIFY, empty_calc, "评述")

    upload = {
        "title": "v.mp4", "authors": [], "published": "", "n_chars": 300,
        "text": "转录" * 100, "notes": [], "kind": "video_upload", "transcript": "转录",
    }
    md_upload = pipeline.render_report("", upload, TRIAGE, VERIFY, empty_calc, "评述")

    return [
        ("混合来源标注了构成", "来源构成" in md_mixed
         and "正文来自上传音视频的转录" in md_mixed),
        ("纯链接不标注构成（避免噪音）", "来源构成" not in md_plain),
        ("内部枚举名不外泄", "上传的音视频" in md_upload and "video_upload" not in md_upload),
    ]


def main() -> int:
    install_fakes()
    body = "某文声称效率提升一百倍，并称这是已证明的定理。" * 20
    events = list(pipeline.analyze("", pasted_text=body, pasted_title="夹具文"))
    bad = 0

    stages = [e["stage"] for e in events]
    for s in ("extract", "triage", "verify", "compute", "done"):
        if s not in stages:
            print(f"[FAIL] 缺少阶段 {s}")
            bad += 1

    if any(e["status"] == "fail" for e in events):
        print("[FAIL] 出现 fail 事件:",
              [e for e in events if e["status"] == "fail"])
        bad += 1

    done = next((e for e in events if e["stage"] == "done"), None)
    if not done or "md" not in (done.get("data") or {}):
        print("[FAIL] 没有 done.md")
        return 1
    md = done["data"]["md"]

    checks = [
        ("实算值进报告", "| 100 |" in md.replace("  ", " ")),
        ("越权表达式被标未实算", "未实算" in md),
        ("作者与主体一节存在", "作者与主体核对" in md),
        ("评述段落存在", "把估算标成了估算" in md),
        ("免责声明存在", "不能替代读者本人对原始出处的核对" in md),
    ]
    checks += _report_header_checks()
    for name, ok in checks:
        print(("[ok] " if ok else "[FAIL] ") + name)
        if not ok:
            bad += 1

    print("\n" + ("端到端通过" if bad == 0 else f"{bad} 项失败"))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
