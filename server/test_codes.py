#!/usr/bin/env python3
"""错误码表自检（P0-a）。

守三件事：

1. **码表自洽**：25 个、编号连续、字段齐全、枚举合法；
2. **两处一致**：异常类声明的 `code` / `notice` 都能在码表里找到；
3. **同 condition 多码是显式的**：DR-1001 与 DR-2001 共用 `incompleteRequest`，
   这是设计决定的（用户动作不同），不能被「优化」成一条。

跑法：python3 server/test_codes.py
退出码：0 全通过 / 1 有失败
"""
from __future__ import annotations

import re
import sys

import asr
import codes
import extract
import llm
import media
import ratelimit
import safe_eval
import search

FAILS: list[str] = []


def chk(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'ok' if cond else '!!'}] {name}" + (f"  {detail}" if not cond and detail else ""))
    if not cond:
        FAILS.append(name)


EXPECTED = [f"DR-{n}" for n in (
    list(range(1001, 1008)) + list(range(2001, 2007)) + list(range(3001, 3004))
    + list(range(4001, 4007)) + list(range(5001, 5004))
)]

print("=" * 70)
print("一、码表自洽")
print("=" * 70)
chk("码数 25", len(codes.CODES) == 25, f"实际 {len(codes.CODES)}")
chk("编号连续无缺漏", list(codes.CODES) == EXPECTED,
    f"缺 {set(EXPECTED) - set(codes.CODES)} 多 {set(codes.CODES) - set(EXPECTED)}")
chk("key 与字典键一致", all(c.key == k for k, c in codes.CODES.items()))

bad = [k for k, c in codes.CODES.items() if not c.message or not c.hint or not c.condition]
chk("文案 / 提示 / condition 均非空", not bad, str(bad))

bad = [k for k, c in codes.CODES.items() if not re.fullmatch(r"[a-z][A-Za-z]*", c.condition)]
chk("condition 为小驼峰", not bad, str(bad))

bad = [k for k, c in codes.CODES.items() if c.scope not in ("pre", "in", "both")]
chk("scope 合法", not bad, str(bad))

bad = [k for k, c in codes.CODES.items() if c.http not in (200, 400, 404, 413, 429, 500, 503)]
chk("http 合法", not bad, str(bad))

bad = [k for k, c in codes.CODES.items() if c.responsibility not in ("user", "third", "us")]
chk("责任方合法", not bad, str(bad))

bad = [k for k, c in codes.CODES.items() if k.startswith("DR-5") and not c.log]
chk("5xxx 一律写日志", not bad, str(bad))

chk("输入类不写日志", all(not c.log for k, c in codes.CODES.items() if k.startswith(("DR-1", "DR-2"))))
chk("3xxx 中只有 3003 写日志",
    [k for k, c in codes.CODES.items() if k.startswith("DR-3") and c.log] == ["DR-3003"])

print()
print("=" * 70)
print("二、payload 与查询")
print("=" * 70)
p = codes.payload("DR-1005")
chk("payload 五个字段齐全",
    set(p) == {"code", "condition", "error", "hint", "retryable"}, str(sorted(p)))
chk("payload 文案取自码表", p["error"] == "正文太短（不足 200 字）", p["error"])
chk("payload 带出 condition", p["condition"] == "textTooShort")
chk("payload 不塞 ok / request_id（由调用方补）", "ok" not in p and "request_id" not in p)

chk("占位符替换（文件上限）", codes.payload("DR-2002", limit=300)["error"].endswith("300 MB"),
    codes.payload("DR-2002", limit=300)["error"])
q = codes.payload("DR-3001", limit=20, minutes=12)
chk("占位符替换（额度文案两处）", "20" in q["error"] and "12" in q["hint"], f"{q['error']} / {q['hint']}")

try:
    codes.get("DR-9999")
    chk("未登记的码直接抛", False)
except KeyError:
    chk("未登记的码直接抛", True)

chk("pre 态码集合包含 1005 不含 4002",
    "DR-1005" in codes.keys_by_scope("pre") and "DR-4002" not in codes.keys_by_scope("pre"))
chk("in 态码集合包含 4002 不含 1005",
    "DR-4002" in codes.keys_by_scope("in") and "DR-1005" not in codes.keys_by_scope("in"))
chk("both 的码两态都在",
    all(k in codes.keys_by_scope("pre") and k in codes.keys_by_scope("in")
        for k, c in codes.CODES.items() if c.scope == "both"))

print()
print("=" * 70)
print("三、异常类 ↔ 码表 一致")
print("=" * 70)
EXPECT_CODE = {
    llm.LLMNotConfigured: "DR-4001",
    llm.LLMError: "DR-4002",
    extract.FetchError: "DR-4003",
    asr.ASRError: "DR-4004",
    media.MediaError: "DR-4005",
    asr.ASRNotConfigured: "DR-4006",
    ratelimit.RateLimiterError: "DR-3003",
}
for cls, want in EXPECT_CODE.items():
    got = getattr(cls, "code", None)
    chk(f"{cls.__name__} → {want}", got == want, f"实际 {got}")

for cls in EXPECT_CODE:
    chk(f"{cls.__name__} 的码在码表里", getattr(cls, "code", None) in codes.CODES)

EXPECT_NOTICE = {
    search.SearchError: "search_unavailable",
    search.SearchNotConfigured: "search_unavailable",
    safe_eval.UnsafeExpression: "calc_untrusted",
    safe_eval.EvalError: "calc_untrusted",
}
for cls, want in EXPECT_NOTICE.items():
    chk(f"{cls.__name__}（降级）→ {want}", getattr(cls, "notice", None) == want,
        f"实际 {getattr(cls, 'notice', None)}")
    chk(f"{cls.__name__} 不应有 code", not hasattr(cls, "code"))

chk("降级标记都在 NOTICES 里", set(EXPECT_NOTICE.values()) <= set(codes.NOTICES))

print()
print("=" * 70)
print("四、异常类总数与去处")
print("=" * 70)
ALL = [llm.LLMError, llm.LLMNotConfigured, extract.FetchError, asr.ASRError,
       asr.ASRNotConfigured, media.MediaError, ratelimit.RateLimiterError,
       search.SearchError, search.SearchNotConfigured,
       safe_eval.UnsafeExpression, safe_eval.EvalError]
chk("异常类共 11 个", len(ALL) == 11, f"实际 {len(ALL)}")
chk("7 个出码", sum(1 for c in ALL if hasattr(c, "code")) == 7,
    f"实际 {sum(1 for c in ALL if hasattr(c, 'code'))}")
chk("4 个降级", sum(1 for c in ALL if hasattr(c, "notice")) == 4,
    f"实际 {sum(1 for c in ALL if hasattr(c, 'notice'))}")
chk("互不重叠", all(not (hasattr(c, "code") and hasattr(c, "notice")) for c in ALL))

print()
print("=" * 70)
print("五、同 condition 多码是显式的")
print("=" * 70)
by_cond: dict[str, list[str]] = {}
for k, c in codes.CODES.items():
    by_cond.setdefault(c.condition, []).append(k)
shared = {c: ks for c, ks in by_cond.items() if len(ks) > 1}
chk("incompleteRequest 由 DR-1001 与 DR-2001 共用",
    shared.get("incompleteRequest") == ["DR-1001", "DR-2001"], str(shared.get("incompleteRequest")))
chk("共用 condition 的码组只有这一组", list(shared) == ["incompleteRequest"], str(list(shared)))

print()
print("=" * 70)
if FAILS:
    print(f"失败 {len(FAILS)} 项：")
    for f in FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("错误码表自检通过")
