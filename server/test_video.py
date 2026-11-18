"""视频号（元宝中转）读解的夹具：验证「二手来源」的标注确实落到报告里。

这条路径的特殊性不在抓取，而在层级：拿到的不是视频原话，是元宝的总结。
若报告不显式声明这一点，读者会把「元宝说视频讲了 X」误当成「视频说 X」。

运行：python3 test_video.py
"""

import sys

import llm
import pipeline

DOC = {
    "url": "https://yb.tencent.com/wx/ct/f/EXAMPLE",
    "kind": "wechat_channel",
    "layer": "L1 二手：元宝对视频的总结（非视频原话）",
    "title": "夹具视频标题",
    "authors": ["某账号"],
    "published": "2026年11月16日",
    "text": "视频号主体：某账号\n\n视频时长：321 秒\n\n元宝生成的视频总结：\n要点一；要点二；" * 4,
    "duration_sec": 321,
    "summary_chars": 302,
    "desc": "文案里声称某专家领衔",
    "notes": [
        "视频号：正文来自元宝对该视频的总结，不是视频原话（L1 二手）",
        "总结 302 字；按时长估口播约 1284–1605 字，覆盖率约 19%–24%",
    ],
    "n_chars": 507,
}

TRIAGE = {
    "genre": "视频口播",
    "genre_reason": "三段要点，无出处",
    "author_note": "账号为搬运方，讲述者为文案所称专家，两者不是同一人。",
    "who_speaks": "账号「某账号」是转述方，实际讲述者为文案中提到的老师。",
    "claims": [{"quote": "这是经典原话", "why": "引用", "kind": "引用"}],
    "transcript_risks": [
        {"as_is": "七胜八益", "should_be": "七损八益", "why": "《素问·阴阳应象大论》作「七损八益」，胜/损 同音易误"},
    ],
    "title_claims": [
        {"claim": "某流派代表性传承人", "status": "未核", "note": "需查官方名录"},
    ],
}
VERIFY = {"items": [{"n": 1, "verdict": "未核", "evidence": "无视频原话可对照", "fix": "标为转述"}]}
CALC = {"items": []}


def install_fakes():
    llm.is_configured = lambda: True
    seq = {"n": 0}

    def fake_json(messages, **kw):
        i = seq["n"]
        seq["n"] += 1
        return [TRIAGE, VERIFY, CALC][min(i, 2)]

    llm.chat_json = fake_json
    llm.chat = lambda messages, **kw: "它讲对了一处：把误区点出来了。最该警惕的是把转述当原话。"
    pipeline.llm = llm


def main() -> int:
    install_fakes()
    bad = 0

    md = pipeline.render_report(
        DOC["url"], DOC, TRIAGE, VERIFY, CALC, pipeline.stage_commentary(DOC, TRIAGE, VERIFY, CALC)
    )

    checks = [
        ("标注来源层级", "来源层级：L1 二手" in md),
        ("标注整理日期非发布时间", "元宝整理日期（非发布时间）" in md),
        ("有「谁在说话」", "谁在说话：" in md and "转述方" in md),
        ("头衔未核列出", "身份/头衔：某流派代表性传承人 —— 未核" in md),
        ("存疑项进「需要回听」节", "需要回听的地方" in md and "七损八益" in md),
        ("覆盖率进报告", "覆盖率约 19%–24%" in md),
        ("免责声明点明二手", "本对象是元宝对视频的总结而非视频原话" in md),
        ("指引上传原视频", "上传原视频即可升为原声读解" in md),
        ("章节编号连续", "## 一、" in md and "## 二、" in md and "## 三、" in md),
    ]
    for name, ok in checks:
        print(("[ok] " if ok else "[FAIL] ") + name)
        if not ok:
            bad += 1

    print("\n" + ("视频号读解通过" if bad == 0 else f"{bad} 项失败"))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
