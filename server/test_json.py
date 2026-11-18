#!/usr/bin/env python3
"""JSON 截断抢救与重试的回归用例。

v1.3 实况：L0 提示词要求「宁可多列」，输出吃满 max_tokens 被切断，
整个任务 fail（用户什么都没拿到）。两道修复：抢救 + 精简重试。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import llm  # noqa: E402
import pipeline  # noqa: E402

# 用户实报的那段截断输出，一字不改
TRUNCATED = '''{
"genre": "视频口播",
"genre_reason": "全文为第一人称口语化讲述，无文献引用与数据出处，属典型短视频口播文案。",
"author_note": "未取到作者信息，正文未声称任何作者或领衔人，无法核对。",
"claims": [
{
"quote": "英伟达从来没有自己的芯片制造厂",
"why": "这是全文立论前提，若英伟达有自有产线则整篇论证崩塌。",
"kind": "事实"
},
{
"quote": "台积电手里的那把刻刀，全世界只有荷兰的阿斯麦一家能造",'''


def main() -> int:
    bad = 0

    def chk(name, cond):
        nonlocal bad
        print(("[ok] " if cond else "[X ] ") + name)
        if not cond:
            bad += 1

    # ---- 一、抢救真实截断样本 ----
    out = llm._salvage_json(TRUNCATED)
    chk("能救回", out is not None)
    chk("体裁保住", (out or {}).get("genre") == "视频口播")
    claims = (out or {}).get("claims") or []
    chk("至少保住 1 条待核项", len(claims) >= 1)
    chk("第一条完整", claims[0].get("kind") == "事实" if claims else False)
    chk("半截的第二条不会带脏数据", len(claims) <= 2)

    # ---- 二、正常 JSON 不走抢救 ----
    ok = '{"a": 1, "b": [2, 3]}'
    chk("完整 JSON 正常解析", llm._salvage_json(ok) == {"a": 1, "b": [2, 3]})

    # ---- 三、截在字符串中间（最容易崩的一种）----
    half = '{"a": "未闭合的字符串'
    chk("截在字符串里不抛异常", llm._salvage_json(half) is None)

    # ---- 四、嵌套数组截断 ----
    nest = '{"items": [{"n": 1}, {"n": 2}, {"n":'
    r = llm._salvage_json(nest)
    chk("嵌套数组救出 2 条", r == {"items": [{"n": 1}, {"n": 2}]})

    # ---- 五、重试机制 ----
    calls = {"n": 0}

    def fake_chat_json(messages, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise llm.LLMError("无法解析为 JSON: 截断")
        return {"genre": "视频口播", "claims": [{"quote": "x", "kind": "事实"}]}

    real = llm.chat_json
    llm.chat_json = fake_chat_json
    pipeline.llm = llm
    try:
        out2, retried = pipeline._json_with_retry([{"role": "user", "content": "t"}], 100, "精简")
        chk("首次失败会重试", calls["n"] == 2)
        chk("重试成功被标记", retried is True)
        chk("重试结果可用", out2.get("genre") == "视频口播")

        calls["n"] = 0

        def fake_ok(messages, **kw):
            calls["n"] += 1
            return {"genre": "视频口播"}

        llm.chat_json = fake_ok
        ok_out, r2 = pipeline._json_with_retry([{"role": "user", "content": "t"}], 100, "精简")
        chk("首次成功不重试", calls["n"] == 1 and r2 is False)
    finally:
        llm.chat_json = real

    print("\n" + ("JSON 健壮性通过" if bad == 0 else f"{bad} 项失败"))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
