#!/usr/bin/env python3
"""五道机器守门（设计文档第十二节，P3 落地）。

设计里说「新增一条错误路径但忘配码，CI 红」——那话要成立，才叫守门。这五条
就是把它兑现：文档↔码表、源码↔码、码↔用例、用例↔文案、两态↔调用位置。

跑法：python3 server/test_guards.py
退出码：0 全通过 / 1 有失败
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import codes

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DOCS = ROOT / "docs" / "ERROR_CODES.md"

# 行为测试：真的会跑起代码、走到错误分支的。`test_codes.py` 不算——
# 它只校验「异常类 ↔ 码」的映射，映射对上不等于有人真的触发过。
BEHAVIOR_TESTS = [
    "test_app.py", "test_pipeline_codes.py", "test_pipeline_e2e.py",
    "test_video.py", "test_extract.py", "test_asr.py", "test_media.py",
    "test_ratelimit.py", "test_search.py", "test_json.py", "test_net.py",
]

FAILS: list[str] = []


def chk(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'ok' if cond else '!!'}] {name}"
          + (f"  {detail}" if not cond and detail else ""))
    if not cond:
        FAILS.append(name)


def _src(name: str) -> str:
    return (HERE / name).read_text(encoding="utf-8")


# ============ 守门一：码表 ↔ 文档双向一致 ============
print("=" * 70)
print("守门一：码表 ↔ 文档")
print("=" * 70)

if not DOCS.exists():
    chk("docs/ERROR_CODES.md 存在", False, "文件不存在")
else:
    doc = DOCS.read_text(encoding="utf-8")
    heads = set(re.findall(r"^### (DR-\d{4}) · ", doc, re.M))
    all_mentions = set(re.findall(r"DR-\d{4}", doc))
    keys = set(codes.CODES)

    chk("段落数 = 码数", len(heads) == len(keys), f"段落 {len(heads)} / 码表 {len(keys)}")
    chk("段落集合 = 码表集合", heads == keys,
        f"文档多出 {sorted(heads - keys)}；码表多出 {sorted(keys - heads)}")
    chk("正文提到的码都在码表里", all_mentions <= keys, f"多出 {sorted(all_mentions - keys)}")

    # 每段四行：用户看到 / 触发 / 何时加 / 排障。
    # 缺行不会报错，只会让后来人少一条线索，所以按段落切块核对。
    blocks = re.split(r"^### ", doc, flags=re.M)[1:]
    bad = [b.split("\n")[0] for b in blocks
           if not all(f"- {k}：" in b for k in ("用户看到", "触发", "何时加", "排障"))]
    chk("每段四行齐全", not bad, f"缺行：{bad}")

# ============ 守门二：源码不出现无码的错误返回 ============
print()
print("=" * 70)
print("守门二：错误返回必须带码（且两态都算）")
print("=" * 70)


def _bare_error_returns(src: str, fname: str) -> list[str]:
    """`_json(4xx|5xx` 走了返回体却没走码表；或者 fail 帧里没有 code。"""
    tree = ast.parse(src)
    bad: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fname_attr = getattr(node.func, "attr", getattr(node.func, "id", ""))
            if fname_attr == "_json" and node.args:
                a0 = node.args[0]
                if isinstance(a0, ast.Constant) and isinstance(a0.value, int) and a0.value >= 400:
                    if not any(k.arg in ("err", "code") for k in node.keywords):
                        bad.append(f"{fname}:{node.lineno} `_json({a0.value}` 没带 err=")
        if isinstance(node, ast.Dict):
            kv = {k.value: v for k, v in zip(node.keys, node.values)
                  if isinstance(k, ast.Constant)}
            st = kv.get("status")
            if isinstance(st, ast.Constant) and st.value == "fail":
                if not any(isinstance(v, ast.Constant) and isinstance(v.value, str)
                           and v.value.startswith("DR-")
                           for v in kv.values()):
                    bad.append(f"{fname}:{node.lineno} fail 帧没有 DR 码")
    return bad


problems = []
for name in ("app.py", "pipeline.py"):
    problems += _bare_error_returns(_src(name), name)
chk("没有无码的错误返回", not problems, "; ".join(problems))

# `_route_get(write_body=...)` 体内的错误返回也要扫到——它在 app.py 里，
# 上面的 AST 遍历本来就覆盖全文，这条只是把「范围含不含函数体」写明白。
chk("_route_get 体内的错误也走码表",
    '_fail("DR-5001", write_body=write_body)' in _src("app.py"),
    "`_route_get` 里读页面失败的分支没走 _fail")

# ============ 守门三：每个码至少一个触发用例 ============
print()
print("=" * 70)
print("守门三：每个码至少一个触发用例")
print("=" * 70)

used: dict[str, list[str]] = {}
# 只认「比较 / 期望」语境里的码：`code == "DR-x"`、`"code": "DR-x"`、
# `assertEqual(..., "DR-x")`。注释里提一句、chk 名字里写一句都不算——
# 那样一条注释就能让守门假装通过（第一版实现就这么被骗过一次）。
ASSERT_CTX = [
    re.compile(r'(?:==|!=)\s*"(DR-\d{4})"'),
    re.compile(r'"code":\s*"(DR-\d{4})"'),
    re.compile(r'assertEqual\([^\n]*b?"(DR-\d{4})"'),
    re.compile(r'assertIn\(\s*b?"(DR-\d{4})"'),
]
for name in BEHAVIOR_TESTS:
    path = HERE / name
    if not path.exists():
        chk(f"{name} 存在", False, "行为测试文件缺失")
        continue
    for line in path.read_text(encoding="utf-8").split("\n"):
        for pat in ASSERT_CTX:
            for c in pat.findall(line):
                used.setdefault(c, [])
                if name not in used[c]:
                    used[c].append(name)

missing = sorted(set(codes.CODES) - set(used))
chk("25 个码都有断言语境的触发用例", not missing, f"没有触发用例：{missing}")

# 顺带看一眼有没有「测试里写着、码表里没有」的码（打字打错就这么来的）
stray = sorted({c for n in BEHAVIOR_TESTS if (HERE / n).exists()
                for c in re.findall(r"DR-\d{4}", (HERE / n).read_text(encoding="utf-8"))}
               - set(codes.CODES))
chk("测试里没有码表外的码", not stray, f"多出：{stray}")

# ============ 守门四：文案不参与断言 ============
print()
print("=" * 70)
print("守门四：错误文案不参与断言（改文案不该牵连测试）")
print("=" * 70)

# 只盯「错误文案」：同一行里既有中文、又在校验 error / hint 字段。
# 不含 ["text"]：那个键在别处是「抓取到的正文」「转录文本」，不是错误文案。
LITERAL_CN = re.compile(r"[\u4e00-\u9fff]")
FIELD = ("[\"error\"]", "[\"hint\"]")
offenders = []
FIRST_ARG = re.compile(r"assert(?:In|Equal)\(\s*([^,]+),")
for name in BEHAVIOR_TESTS:
    path = HERE / name
    if not path.exists():
        continue
    for i, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1):
        if not line.lstrip().startswith(("self.assert", "assert ")):
            continue
        if not any(f in line for f in FIELD):
            continue
        # 只看断言的**第一个参数**：那是「期望值」。失败消息里的中文不算违规，
        # 否则「写清楚为什么断言失败」反而会被这条守门惩罚。
        m = FIRST_ARG.search(line)
        if m and LITERAL_CN.search(m.group(1)):
            offenders.append(f"{name}:{i} {line.strip()[:70]}")
chk("没有断言错误文案的测试", not offenders, "; ".join(offenders))

# ============ 守门五：stage_scope 与调用位置一致 ============
print()
print("=" * 70)
print("守门五：两态与调用位置一致")
print("=" * 70)


def _codestr(node) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


pre_side: list[tuple[str, str, int]] = []    # 流前：_fail(...)
in_side: list[tuple[str, str, int]] = []     # 流中：status=fail 的帧

for name in ("app.py", "pipeline.py"):
    tree = ast.parse(_src(name))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if getattr(node.func, "attr", "") == "_fail" and node.args:
                c = _codestr(node.args[0])
                if c:
                    pre_side.append((c, name, node.lineno))
        if isinstance(node, ast.Dict):
            kv = {k.value: v for k, v in zip(node.keys, node.values)
                  if isinstance(k, ast.Constant)}
            st = kv.get("status")
            code_node = kv.get("code")
            if isinstance(st, ast.Constant) and st.value == "fail" and code_node is not None:
                c = _codestr(code_node)
                if c:
                    in_side.append((c, name, node.lineno))

wrong = []
for c, name, ln in pre_side:
    if c in codes.CODES and codes.CODES[c].scope == "in":
        wrong.append(f"{name}:{ln} 流前用了标 in 的 {c}")
for c, name, ln in in_side:
    if c in codes.CODES and codes.CODES[c].scope == "pre":
        wrong.append(f"{name}:{ln} 流中用了标 pre 的 {c}")
chk("标 pre 的码不出现在流中，标 in 的不出现在流前", not wrong, "; ".join(wrong))

# 反向：每个码至少有一条真实出现（不是死码）。scope 说「可能出现在哪」，
# 若两边都没出现，说明这个码标了用不上。
appeared = {c for c, _, _ in pre_side} | {c for c, _, _ in in_side}
dead = sorted(set(codes.CODES) - appeared)
chk("码表里没有死码（每个码都有实际发出的地方）", not dead, f"从未发出：{dead}")

print()
print("=" * 70)
if FAILS:
    print(f"失败 {len(FAILS)} 项：")
    for f in FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("五道守门全部通过")
