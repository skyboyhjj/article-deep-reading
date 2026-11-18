#!/usr/bin/env python3
"""前后端契约测试：服务端发的 stage / status，前端必须都认识。

这不是单元测试，是防「静默不同步」。前端 `setStage()` 里有一句
`if(!el) return;` —— 服务端发一个前端没有的 stage，界面**一点反应都没有**，
不报错、不提示，看起来就像卡住了。v2.12 实测就是这样发现「成稿」标签
永远停在「…」的（服务端只发了 render 的 run，随后直接 done）。

本测试只看两件事：
  ① pipeline 会发的 stage 集合，与 index.html 的标签集合是否互为子集；
  ② 每个 stage 是否都有「开始 + 结束」两种状态（只有 run 没有 ok/warn/fail，
     等于界面上永远转圈）。
"""
import ast
import os
import re
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
INDEX = HERE.parent / "web" / "index.html"
PIPELINE = HERE / "pipeline.py"

# 前端显式吞掉、不进标签的 stage
ENGINE_ONLY = {"done", "quota", "error"}
# 前端 handleEvent 里处理的 status（info 只更新日志行，不改标签）
KNOWN_STATUS = {"run", "ok", "warn", "fail", "info"}


def _constants(node) -> list[str]:
    """把字面量或三元表达式里能确定的分支都取出来。

    `"status": "ok" if cond else "warn"` 是 IfExp，不是 Constant ——
    只认 Constant 会把 compute 误判成「只有 run、没有结束态」。
    """
    if isinstance(node, ast.Constant):
        return [str(node.value)]
    if isinstance(node, ast.IfExp):
        return _constants(node.body) + _constants(node.orelse)
    return []


def pipeline_events() -> list[tuple[str, str]]:
    """从 AST 取出所有 {'stage': X, 'status': Y} 字面量。"""
    tree = ast.parse(PIPELINE.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        kv = {}
        for k, v in zip(node.keys, node.values):
            if isinstance(k, ast.Constant):
                kv[k.value] = v
        if "stage" not in kv:
            continue
        stages = _constants(kv["stage"]) or [""]
        statuses = _constants(kv.get("status")) if "status" in kv else []
        for s in stages:
            for st in statuses or [""]:
                out.append((s, st))
    return out


def html_tags() -> set[str]:
    html = INDEX.read_text(encoding="utf-8")
    tags = set(re.findall(r'data-s="([a-z_]+)"', html))
    label = re.search(r"const LABEL = \{(.*?)\};", html, re.S)
    keys = set()
    if label:
        keys = set(re.findall(r"([a-z_]+)\s*:", label.group(1)))
    return tags, keys


class TestStageContract(unittest.TestCase):
    def setUp(self):
        self.events = pipeline_events()
        self.stages = {s for s, _ in self.events}
        self.statuses = {st for _, st in self.events}
        self.tags, self.labels = html_tags()

    def test_pipeline_parsed(self):
        self.assertGreater(len(self.events), 10, "没能从 pipeline.py 解析出事件字面量")

    def test_every_backend_stage_has_a_tag(self):
        missing = (self.stages - ENGINE_ONLY) - self.tags
        self.assertEqual(missing, set(),
                         f"服务端会发这些 stage，但 index.html 没有对应标签：{missing}")

    def test_every_tag_is_actually_used(self):
        unused = self.tags - self.stages
        self.assertEqual(unused, set(),
                         f"index.html 有这些标签，但服务端从不发，它们永远不会亮：{unused}")

    def test_labels_match_tags(self):
        self.assertEqual(self.tags, self.labels,
                         "data-s 标签与 LABEL 表的键不一致，标签会显示成 undefined")

    def test_statuses_known(self):
        unknown = self.statuses - KNOWN_STATUS
        self.assertEqual(unknown, set(), f"前端没处理这些 status：{unknown}")

    def test_each_stage_has_terminal_state(self):
        """只有 run 没有结束态的 stage，在界面上会永远转圈 —— v2.12 的 render 就是这样。"""
        by_stage: dict[str, set[str]] = {}
        for s, st in self.events:
            by_stage.setdefault(s, set()).add(st)
        stuck = [s for s in self.tags
                 if "run" in by_stage.get(s, set())
                 and not (by_stage.get(s, set()) & {"ok", "warn", "fail"})]
        self.assertEqual(stuck, [], f"这些 stage 只发 run 不发结束态，界面会一直转圈：{stuck}")

    def test_done_carries_md(self):
        """前端靠 done.data.md 渲染报告，少了它报告区不出现。"""
        src = PIPELINE.read_text(encoding="utf-8")
        m = re.search(r'"stage": "done".{0,200}', src, re.S)
        self.assertTrue(m, "找不到 done 事件")
        self.assertIn('"md": md', m.group(0), "done 事件没有带 md")



class TestReportLayers(unittest.TestCase):
    """v2.17 分层展示：简约 / 详细 / 原文三档，错一档就有一屏空白。

    这一组不是查逻辑，是查「线接错了没有」——tab 与容器名对不上时，
    点击没反应、或点了整块空白，浏览器不报错，只能靠契约卡住。
    """

    KEYS = {"brief": "bview", "view": "rview", "raw": "report"}

    def setUp(self):
        self.src = PIPELINE.read_text(encoding="utf-8")
        self.html = INDEX.read_text(encoding="utf-8")

    def test_done_carries_brief(self):
        m = re.search(r'"stage": "done".{0,240}', self.src, re.S)
        self.assertTrue(m, "找不到 done 事件")
        self.assertIn('"brief": brief', m.group(0), "done 事件没带简约版")

    def test_three_tabs_all_have_containers(self):
        for key, box in self.KEYS.items():
            self.assertIn(f'data-v="{key}"', self.html, f"缺 tab：{key}")
            self.assertIn(f'id="{box}"', self.html, f"缺容器：{box}")

    def test_default_tab_is_brief(self):
        """默认必须是简约版：一次全给等于把判断和依据堆一屏，读者只看前者。"""
        self.assertRegex(self.html, r'class="tab on"[^>]*data-v="brief"')

    def test_showtab_switches_all_three(self):
        m = re.search(r"function showTab\((\w+)\)\{(.*?)\n\}", self.html, re.S)
        self.assertTrue(m, "找不到 showTab")
        body = m.group(2)
        for key, box in self.KEYS.items():
            self.assertIn(box, body, f"showTab 没管 {box}")
            self.assertIn(f'"{key}"', body, f"showTab 没处理 {key}")


if __name__ == "__main__":
    unittest.main()
