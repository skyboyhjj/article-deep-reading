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

import codes

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

    def test_hero_tail_is_unbreakable(self):
        """标题 18 个全角字符，窄屏一行放不下（378px > 332px）。

        断点必须锁在逗号后：后半句整体不拆，两行各 9 字才均衡；
        否则浏览器会在「你」「的」之间乱断。
        """
        self.assertIn('<span class="h1-tail">你的数据环保卫士。</span>', self.html)
        m = re.search(r"\.h1-tail\{[^}]*white-space:\s*nowrap", self.html)
        self.assertTrue(m, "h1-tail 没有 nowrap，窄屏会在句中乱断")

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
        self.assertIn("你的数据你做主", m.group(1))
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
        self.assertIn("hui-skill.org/ethics#data-rules", self.html,
                      "页脚应直达伦理页的「数据四条铁律」块")
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


def css_var(html: str, name: str) -> str | None:
    m = re.search(r"--" + re.escape(name) + r"\s*:\s*(#[0-9a-fA-F]{3,6})", html)
    return m.group(1) if m else None


def css_rule(html: str, selector: str) -> dict:
    """取选择器的 color 与 background，支持 var(--x)。"""
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", html)
    if not m:
        return {}
    out = {}
    for prop in ("color", "background"):
        mm = re.search(r"(?<![-\w])" + prop
                       + r"\s*:\s*(#[0-9a-fA-F]{3,6}|var\(--[\w-]+\))", m.group(1))
        if not mm:
            continue
        v = mm.group(1)
        if v.startswith("var("):
            v = css_var(html, v[6:-1])
        if v:
            out[prop] = v
    return out


class TestErrorPresentation(unittest.TestCase):
    """P1：错误提示按责任分色、码与诊断信息能一键复制（设计 v2.1 第八节）。

    守的不是「样式好不好看」，而是三件会静默坏掉的事：

      ① 前端又自己算了一遍分档 —— 与码表漂开，只有浏览器里才看得出；
      ② 某条错误漏走统一出口 —— 还在往 #err 塞纯文本，码与诊断全丢；
      ③ 前端写了码表里没有的码 —— 动作按钮点了没反应，诊断也对不上日志。
    """

    def setUp(self):
        self.html = INDEX.read_text(encoding="utf-8")
        m = re.search(r"<script>(.*)</script>", self.html, re.S)
        self.assertTrue(m, "找不到内联脚本")
        self.js = m.group(1)

    def test_frontend_uses_server_side_presentation(self):
        """红/黄、摆不摆码、摆不摆诊断，都由服务端 codes.present() 定。"""
        for field in ("E.level", "E.show_code", "E.show_diag"):
            self.assertIn(field, self.js,
                          f"前端没有用服务端给的 {field}，等于又把分档算了一遍")

    def test_only_two_levels(self):
        self.assertIn(".err.fail .body", self.html)
        self.assertIn(".err.warn .body", self.html)

    def test_no_direct_err_writes(self):
        """所有错误都必须经 showErr —— 直接塞 textContent 会把码和诊断一起丢掉。"""
        self.assertEqual(self.js.count('$("err").textContent'), 0,
                         "还有地方直接往 #err 写文本，绕过了 showErr")

    def test_error_action_codes_exist(self):
        m = re.search(r"const ERR_ACTION = \{(.*?)\n\};", self.js, re.S)
        self.assertTrue(m, "找不到 ERR_ACTION 表")
        used = set(re.findall(r'"(DR-\d{4})"', m.group(1)))
        self.assertTrue(used, "ERR_ACTION 是空的")
        self.assertEqual(used - set(codes.CODES), set(), "动作按钮绑了码表里没有的码")

    def test_every_code_in_html_exists(self):
        """前端出现的每个 DR-xxxx 都得在码表里，否则那一支永远走不到对的文案。"""
        used = set(re.findall(r"DR-\d{4}", self.html))
        self.assertTrue(used)
        self.assertEqual(used - set(codes.CODES), set())

    def test_diag_has_five_lines(self):
        """诊断信息五行：版本与时间、码与 condition、阶段与追踪号、状态与耗时、UA。"""
        m = re.search(r"function buildDiag\(E\)\{(.*?)\n\}", self.js, re.S)
        self.assertTrue(m, "找不到 buildDiag")
        body = m.group(1)
        for key in ("DeepRead", "code:", "stage:", "status:", "ua:"):
            self.assertIn(key, body, f"诊断信息缺 {key} 这一段")

    def test_copy_works_without_clipboard_api(self):
        """局域网 IP 直连（http，非 localhost）没有 navigator.clipboard，必须留退路。"""
        self.assertIn("navigator.clipboard", self.js)
        self.assertIn("execCommand", self.js)

    def test_countdown_is_cleared(self):
        m = re.search(r"function clearErr\(\)\{(.*?)\n\}", self.js, re.S)
        self.assertTrue(m, "找不到 clearErr")
        self.assertIn("clearInterval", m.group(1), "清错误时没停倒计时，会留下一个跑着的 timer")

    def test_error_paths_all_go_through_showerr(self):
        """上传失败 / 请求失败 / 流中失败 / 前端自判 / 网络错误 / 健康检查，都要展示。"""
        self.assertGreaterEqual(self.js.count("showErr("), 7,
                                "showErr 的调用点太少，有错误路径还在自己拼文案")

    def test_err_colors_meet_wcag(self):
        for sel in (".err.fail .body", ".err.warn .body"):
            r = css_rule(self.html, sel)
            self.assertIn("color", r, f"{sel} 取不到文字色")
            self.assertIn("background", r, f"{sel} 取不到底色")
            ratio = contrast_ratio(r["color"], r["background"])
            self.assertGreaterEqual(
                ratio, 4.5,
                f"{sel} 对比度 {ratio} < 4.5（{r['color']} on {r['background']}）")


class TestVersion(unittest.TestCase):
    """版本号只该有一个真源。

    `APP_VERSION` 出去的地方有三处（`/api/health`、SSE 首帧、用户复制的诊断信息），
    而发版时最容易忘的是 CHANGELOG。任其漂开，用户报障时报的版本就对不上代码。
    """

    def test_app_version_matches_changelog(self):
        import app
        changelog = (HERE.parent / "CHANGELOG.md").read_text(encoding="utf-8")
        m = re.search(r"^## v([\d.]+) ·", changelog, re.M)
        self.assertTrue(m, "CHANGELOG 里找不到 `## vX.Y ·` 形式的首个版本标题")
        self.assertEqual(m.group(1), app.APP_VERSION,
                         "CHANGELOG 首个版本号与 app.APP_VERSION 不一致，发版时漏改了一处")

    def test_version_comment_names_a_real_test_file(self):
        """注释里点名的测试文件必须真的在。

        来由：app.py 的版本注释曾写「test_web 会比对」，而实际测试落在本文件，
        读者按注释去找 test_web.py 找不到，就会误判「这个守门不存在」。
        注释指向哪儿，哪儿就得有东西。
        """
        src = (HERE / "app.py").read_text(encoding="utf-8")
        m = re.search(r"#.*CHANGELOG.*同号（(.*?)）", src)
        self.assertTrue(m, "app.py 里找不到「CHANGELOG 首个标题必须同号」的注释")
        names = set(re.findall(r"(test_[a-z_]+)", m.group(1)))
        self.assertTrue(names, "注释里没点名任何测试文件")
        for name in names:
            self.assertTrue((HERE / f"{name}.py").exists(),
                            f"注释指向 {name}.py，但 server/ 下没有这个文件")


if __name__ == "__main__":
    unittest.main()
