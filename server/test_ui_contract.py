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


def _rgb(h: str) -> tuple:
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _luminance(c: tuple) -> float:
    f = lambda v: (v / 255 / 12.92) if v / 255 <= 0.03928 else ((v / 255 + 0.055) / 1.055) ** 2.4
    r, g, b = c
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def contrast_ratio(fg: str, bg: str) -> float:
    """WCAG 对比度。小号文字（<18.66px 粗体 / <24px）要求 ≥ 4.5。"""
    la, lb = _luminance(_rgb(fg)), _luminance(_rgb(bg))
    hi, lo = max(la, lb), min(la, lb)
    return round((hi + 0.05) / (lo + 0.05), 2)


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


class TestHomeTheme(unittest.TestCase):
    """v2.22 首页改版：慧惠口径 + 数据去向。

    这里的断言都是「文案与代码事实对得上」——首页写出去的每条承诺，都要能在
    代码里指到位置，否则就是把伦理写成广告。
    """

    def setUp(self):
        self.html = INDEX.read_text(encoding="utf-8")

    def test_hero_present(self):
        self.assertIn("你好呀，我是慧惠", self.html)
        self.assertIn("我只读一遍，不留一片垃圾", self.html)

    def test_hero_states_the_role(self):
        """一句话说清「我是谁、我为你做什么」。"""
        self.assertIn("你的数据环保卫士", self.html)

    def test_primary_button_names_the_actor(self):
        """按钮主语是慧惠，不是含糊的「帮我」。"""
        m = re.search(r'<button id="go">([^<]*)</button>', self.html)
        self.assertTrue(m, "找不到主按钮")
        self.assertEqual(m.group(1).strip(), "慧惠读解")

    def test_upload_area_says_why(self):
        """上传区要交代用途（原声读解），而不是只放一个按钮。"""
        self.assertIn("或原声读解", self.html)
        self.assertNotIn("从微信「保存视频」导出后传上来", self.html)

    def test_data_card_is_collapsed(self):
        """数据去向必须折叠，且默认收起 —— 首屏留给「能帮你做什么」。"""
        m = re.search(r'<details[^>]*class="[^"]*data[^"]*"[^>]*>(.{0,120})', self.html, re.S)
        self.assertTrue(m, "找不到数据去向折叠块")
        self.assertIn("读解如何尊重你的隐私", m.group(1))
        self.assertNotIn(" open", m.group(0), "数据去向不该默认展开")

    def test_disclaimer_says_input_not_body(self):
        self.assertIn("依据输入内容生成", self.html)
        self.assertNotIn("所给正文", self.html)

    def test_drop_button_is_wired(self):
        """删除按钮必须真的接上 /api/upload/drop，否则是个死按钮。"""
        self.assertIn('id="dropBtn"', self.html)
        m = re.search(r'\$\("dropBtn"\)\.addEventListener\("click".{0,400}', self.html, re.S)
        self.assertTrue(m, "dropBtn 没有绑定点击")
        self.assertIn("/api/upload/drop", m.group(0))

    def test_footer_links_to_ethics(self):
        """四条铁律归拢到伦理页 —— 首页只留入口。"""
        self.assertIn("hui-skill.org/ethics", self.html)
        self.assertNotIn("四条铁律", self.html)

    def test_stages_hidden_until_run(self):
        """阶段条初始不显示：第一次来的人不该先面对七个陌生标签。"""
        self.assertRegex(self.html, r'id="stages"[^>]*style="[^"]*display:\s*none')
        m = re.search(r"function resetStages\(\)\{(.{0,200})", self.html, re.S)
        self.assertTrue(m, "找不到 resetStages")
        self.assertIn('$("stages").style.display', m.group(1), "resetStages 没显示阶段条")

    # ---- 阶段标签的配色对比度（v2.22）----
    def _var(self, name):
        m = re.search(r"--" + re.escape(name) + r"\s*:\s*(#[0-9a-fA-F]{3,6})", self.html)
        return m.group(1) if m else None

    def _rule(self, selector):
        """取选择器的 color 与 background，支持 var(--x)。"""
        m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", self.html)
        if not m:
            return {}
        body = m.group(1)
        out = {}
        for prop in ("color", "background"):
            # 前置断言避免把 border-color 当成 color
            mm = re.search(r"(?<![-\w])" + prop + r"\s*:\s*(#[0-9a-fA-F]{3,6}|var\(--[\w-]+\))", body)
            if not mm:
                continue
            v = mm.group(1)
            if v.startswith("var("):
                v = self._var(v[6:-1])
            if v:
                out[prop] = v
        return out

    def test_stage_colors_meet_wcag(self):
        """五态文字对比度都得过 AA 4.5。

        原稿两处不达标：未跑态 #888 只有 3.5、warn 态 #a86a12 只有 4.13 ——
        而 warn 恰恰是「跑完但有话说」，最该看清的一档。
        """
        for sel in (".st", ".st.run", ".st.ok", ".st.warn", ".st.fail"):
            r = self._rule(sel)
            self.assertIn("color", r, f"{sel} 取不到文字色")
            self.assertIn("background", r, f"{sel} 取不到底色")
            ratio = contrast_ratio(r["color"], r["background"])
            self.assertGreaterEqual(
                ratio, 4.5, f"{sel} 对比度 {ratio} < 4.5（{r['color']} on {r['background']}）")

    def test_stage_run_distinguished_by_motion(self):
        """进行中与完成态底色只差 RGB 3.6，必须有动效或别的区分手段。"""
        m = re.search(r"\.st\.run\s*\{([^}]*)\}", self.html)
        self.assertTrue(m, "找不到 .st.run")
        self.assertIn("animation", m.group(1), "运行态没有动效，与完成态几乎同色")
        self.assertRegex(self.html, r"@keyframes\s+stpulse")
        self.assertIn("prefers-reduced-motion", self.html, "动效缺少减少动态的降级")


if __name__ == "__main__":
    unittest.main()
