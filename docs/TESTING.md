# 测试说明（给独立验证者）

> 版本：对应当前 v2.12 | 最后更新：2026-11-18
> 用途：让另一个人（或另一个 AI 工具）**独立跑一遍**本项目的测试，并逐条核对结果是否与本文档所述一致。

---

## 一、这份文档要验证什么

不是验证「代码写得好不好」，只验证三件事：

1. **17 套测试能不能在干净环境里全部跑通**（不联网、不需要 API key）；
2. **输出是否与本文档的预期一致**（项数、末行结论）；
3. **测试是否真的有效** —— 故意把某处改坏，对应测试应当变红（第七节给了三个可复现的实验）。

第 3 条最重要。一套永远绿的测试，可能是空的。

---

## 二、环境要求

| 项 | 要求 | 缺了会怎样 |
| :-- | :-- | :-- |
| Python | 3.12 或以上 | 语法用了 `str \| None`，3.9 以下会报错 |
| `requests` | `pip install -r requirements.txt` | 全部测试失败 |
| `numpy` | `pip install numpy` | **`test_voice` 直接 ImportError**（服务本身不需要它，只有语音画像分析需要） |
| `ffmpeg` + `ffprobe` | 可选 | `test_asr` 打印 `[skip]` 仍算通过；`test_media` 的 2 个真实用例跳过（`skipped=2`），其余 32 项照跑 |
| 网络 | **不需要** | 全部测试都不联网 |
| API key | **不需要** | 测试用假模型替身 |

一句话：**有 Python 3.12 + requests + numpy 就能跑**，ffmpeg 只是让两个用例从「跳过」变成「真跑」。

### 安装

```bash
pip install -r requirements.txt
pip install numpy
```

Windows 上 instal numpy 若失败，用 `pip install numpy --only-binary :all:`。

---

## 三、怎么跑

### 方式一：一条一条跑（推荐首次验证）

```bash
cd article-deep-reading
python3 server/test_safe_eval.py
python3 server/test_app.py
python3 server/test_ui_contract.py
python3 server/test_extract.py
python3 server/test_pipeline_e2e.py
python3 server/test_video.py
python3 server/test_media.py
python3 server/test_asr_backend.py
python3 server/test_asr.py
python3 server/test_audit.py
python3 server/test_search.py
python3 server/test_json.py
python3 server/test_net.py
python3 server/test_doctor.py
python3 server/test_voice.py
python3 server/test_replay_v10.py --assert
```

Windows 把 `python3` 换成 `python`。

### 方式二：一键跑全部并自动比对（推荐快速判断）

**bash / macOS / Linux**

```bash
cd article-deep-reading
fail=0
for t in test_safe_eval test_app test_ui_contract test_extract test_pipeline_e2e \
         test_video test_media test_asr_backend test_asr test_audit test_search \
         test_json test_net test_doctor test_voice; do
  if python3 server/$t.py >/tmp/$t.log 2>&1; then echo "PASS  $t"; else echo "FAIL  $t"; fail=1; fi
done
if python3 server/test_replay_v10.py --assert >/tmp/replay.log 2>&1; then echo "PASS  test_replay_v10 --assert"; else echo "FAIL  test_replay_v10 --assert"; fail=1; fi
echo "---- 汇总：$([ $fail -eq 0 ] && echo 全通过 || echo 有失败)"
```

**PowerShell（Windows）**

```powershell
cd article-deep-reading
$fail = 0
$tests = @("test_safe_eval","test_app","test_ui_contract","test_extract","test_pipeline_e2e",
           "test_video","test_media","test_asr_backend","test_asr","test_audit","test_search",
           "test_json","test_net","test_doctor","test_voice")
foreach ($t in $tests) {
    python "server/$t.py" *> "$env:TEMP\$t.log"
    if ($LASTEXITCODE -eq 0) { Write-Host "PASS  $t" } else { Write-Host "FAIL  $t"; $fail = 1 }
}
python "server/test_replay_v10.py" --assert *> "$env:TEMP\replay.log"
if ($LASTEXITCODE -eq 0) { Write-Host "PASS  test_replay_v10 --assert" } else { Write-Host "FAIL  test_replay_v10 --assert"; $fail = 1 }
Write-Host ("---- 汇总：" + $(if ($fail -eq 0) { "全通过" } else { "有失败" }))
```

### 预期总耗时

本机实测合计约 **11 秒**（最慢的是 `test_net`，4.4 秒 —— 它在测 TLS 出口的重试与错误翻译，含人为等待）。
若超过 5 分钟没跑完，说明某处卡住了（多半是环境问题，不是测试设计）。

---

## 四、17 套测试逐条说明

分两类：**unittest 式**（输出 `Ran N tests` 与 `OK`）与**脚本式**（打印 `[ok]` 行与一句结论）。

| # | 文件 | 类型 | 项数 | 测什么 | 期望末行 |
| --: | :-- | :-- | --: | :-- | :-- |
| 1 | `test_safe_eval.py` | 脚本 | 35 | 白名单求值器：12 个正确算式 + 18 个攻击用例 + 5 个失败用例 | `全部通过 / 共 35 项` |
| 2 | `test_app.py` | unittest | 46 | HTTP 层：真起服务打真请求（路由、health 不泄 key、analyze 八条校验、SSE 形状、上传六种、限流、并发额度）；上传件 TTL 清扫 6 项（超期删/未超期留/目录不在/只删超期/计数/上传即扫）；删除接口 5 项（正常删/幂等/非法 id/删后分析 404/不耗额度） | `Ran 46 tests` + `OK` |
| 3 | `test_ratelimit.py` | unittest | 20 | 限流器：滑动窗口逐笔恢复、Retry-After 取最早一笔、热配置覆盖、键为 32 位哈希且不含明文 IP（内存实现同样）、三态返回、Redis 故障即 unavailable、熔断与恢复、按 `REDIS_URL` 选实现 | `Ran 20 tests` + `OK` |
| 4 | `test_ui_contract.py` | unittest | 16 | 前后端契约：stage 集合双向核对、LABEL 与 data-s 一致、每阶段必须有结束态、done 必带 md 与 brief、三档渲染（简约/详细/原文）的 tab 与容器接线、默认档为简约；首页主题五条（hero 存在、数据卡默认收起、免责声明说「输入内容」、删除按钮真接线、伦理页入口在而四条铁律正文不在） | `Ran 16 tests` + `OK` |
| 5 | `test_extract.py` | unittest | 11 | 抓取层：`FETCH_BACKEND` 三档与未设置、通用网页、公众号 `pre` 不粘连、公式还原、UTC+8、回退 | `Ran 16 tests` + `OK` |
| 6 | `test_pipeline_e2e.py` | 脚本 | 25 | 五阶段数据流（假模型替身）：阶段齐全、实算值进报告、越权表达式标未实算；报告头三查（混合来源标注、纯链接不标注、内部枚举名不外泄）；分层六查（先说可信部分、AI 身份、指向详细版、更短、不含表格、详细版仍有表）；结论三档（全确证 / 多数不实 / 判定不明）与「分项之和恒等于可核条数」；引文按 `n` 从 claims 回填、降级项不计入问题数而单列「出处不足」、降级项 fix 不再写「无需修改」 | `端到端通过` |
| 7 | `test_video.py` | 脚本 | 9 | 视频号路径：L1 二手标注、来源层级声明、总结覆盖率的算术 | `视频号读解通过` |
| 8 | `test_media.py` | unittest | 34 | 媒体层：假 ffmpeg 覆盖全部失败分支与命令形状；真 ffmpeg 用例另设 `skipUnless` | `Ran 34 tests` + `OK`（无 ffmpeg 时 `skipped=2`） |
| 9 | `test_asr_backend.py` | unittest | 29 | ASR 适配层：四档后端判定、413/429/5xx/非 JSON/连接异常、`verbose_json` 降级、本地 whisper 三种失败、分片偏移三档回退 | `Ran 29 tests` + `OK` |
| 10 | `test_asr.py` | 脚本 | 18 | 转录链路：抽音轨、切片、偏移合并、L1→L0 升级、报告节、规则替换、校对失败兜底 | `ASR 链路通过` |
| 11 | `test_audit.py` | 脚本 | 44 | 自产报告审计回归：空表、假算、三态判定混乱 | `审计回归通过` |
| 12 | `test_search.py` | 脚本 | 31 | 溯源与权威判定：域名白名单、低质量不升级、二次 `site:` 补检 | `Search 链路通过` |
| 13 | `test_json.py` | 脚本 | 12 | JSON 健壮性：截断抢救、精简重试、剥 ```json 壳 | `JSON 健壮性通过` |
| 14 | `test_net.py` | 脚本 | 18 | TLS 出口：CA 优先级、重试、错误翻译 | `TLS 出口通过` |
| 15 | `test_doctor.py` | unittest | 13 | 体检脚本：代理脱敏（密码不许出现）、`INSECURE_TLS` 告警、certifi 缺失文案、TLS 三结局、无网跑完 | `Ran 13 tests` + `OK` |
| 16 | `test_voice.py` | 脚本 | 16 | 语音画像：声学 + 文本双证据，实测与推断分栏 | `语音画像通过` |
| 17 | `test_replay_v10.py --assert` | 脚本 | 16 | 历史误判回放：v1.0–v1.6 出过的错（6 类），逐条断言不许回来 | `v1.0 回放回归通过` |

合计 **393 个断言点**：35（safe_eval）+ 46 + 20 + 16 + 11 + 25 + 9 + 34 + 29 + 18 + 44 + 31 + 12 + 18 + 13 + 16 + 16。

其中 `test_safe_eval` 的 35 项是脚本自己报的（末行写「共 35 项」），其余 12 套的项数可用 `[ok]` 行数或 `Ran N tests` 直接数出来。

### 判定 PASS 的统一标准

1. **退出码为 0**（`echo $?` 或 `$LASTEXITCODE` 为 0）；
2. **末行与上表一致**；
3. 没有出现 `FAIL`、`FAILED`、`[X ]` 字样。

三者任一不满足，按第六节的清单定位。

---

## 五、两个容易误判的地方

### 1. `test_asr.py` 与 `test_media.py` 在无 ffmpeg 时输出不同

- `test_asr.py`：打印 `[skip] 无 ffmpeg，跳过媒体用例`，但**结论仍是 `ASR 链路通过`** —— 这是设计如此，不算失败。
- `test_media.py`：仍然 `Ran 34 tests ... OK`，但末尾会显示 `skipped=2`（两个真实 ffmpeg 用例跳过）。

**请勿把 skip 当成失败。** 但也请注意：skip 的那部分确实没被验证 —— 这就是 v2.7 修过的坑（原来 `test_asr` 的媒体用例在无 ffmpeg 时整段跳过，等于占位）。

### 2. `unittest` 的输出在 stderr

`Ran N tests` 与 `OK` 由 unittest 写到 **stderr**。若用 `python ... > out.txt` 只重定向 stdout，你会看到一个空文件却以为没跑。

正确写法：`> out.txt 2>&1`（bash）或 `*> out.txt`（PowerShell）。

---

## 六、逐条核对清单

跑完后逐项打勾。右列是「该项失败时最可能的原因」。

- [ ] `test_safe_eval` 末行 `全部通过 / 共 35 项` —— 求值器白名单被放宽，或被攻击用例绕过（**阻断级**）
- [ ] `test_app` `Ran 35 tests ... OK` —— 端口绑定失败（换机器/防火墙）、`127.0.0.1:0` 不允许、限流计数未清
- [ ] `test_ui_contract` `Ran 11 tests ... OK` —— 改了 `pipeline.py` 的 stage 名或 `index.html` 的标签却没同步
- [ ] `test_extract` `Ran 11 tests ... OK` —— 抓取层改动（尤其 `fetch()` 里的环境变量分支）
- [ ] `test_pipeline_e2e` 末行 `端到端通过`（内部 20 项 [ok]）—— 五阶段数据流断裂、报告模板改动、来源构成标注被删
- [ ] `test_video` 末行 `视频号读解通过` —— L1 二手标注被删、覆盖率算术改动
- [ ] `test_media` `Ran 34 tests ... OK` —— ffmpeg 命令形状改动（如 wav 的 `pcm_s16le` 被换回 `copy`）
- [ ] `test_asr_backend` `Ran 29 tests ... OK` —— 后端判定或错误码映射改动
- [ ] `test_asr` 末行 `ASR 链路通过` —— 切片、偏移合并、L1→L0 升级改动
- [ ] `test_audit` 末行 `审计回归通过` —— 报告渲染或五档判定改动
- [ ] `test_search` 末行 `Search 链路通过` —— 权威判定白名单改动
- [ ] `test_json` 末行 `JSON 健壮性通过` —— 截断抢救逻辑改动
- [ ] `test_net` 末行 `TLS 出口通过` —— CA 优先级或重试策略改动（本项最慢，约 4 秒）
- [ ] `test_doctor` `Ran 13 tests ... OK` —— 体检文案改动、`verify_message` 兜底被删
- [ ] `test_voice` 末行 `语音画像通过` —— **先确认装了 numpy**；否则是指标方向写反
- [ ] `test_replay_v10 --assert` 末行 `v1.0 回放回归通过` —— 判定档位被放宽（**最严重的一类**）

---

## 七、验证「测试是否真的有效」（强烈建议做）

一套永远绿的测试可能是空的。挑下面三个实验，每个都能在 1 分钟内完成 —— **改坏 → 跑 → 看它变红 → 改回 → 看它变绿**。

### 实验一：抓取层漏 import

1. 打开 `server/extract.py`，删掉 `import os` 那一行（在 `import html as htmllib` 之后、`import re` 之前）；
2. 跑 `python3 server/test_extract.py` → **期望 `Ran 11 tests` 且 `FAILED (errors=...)`**；
3. 把 `import os` 加回去 → **期望 `OK`**。

若删掉 `import os` 后测试仍然绿，说明这套测试没覆盖真实抓取路径（v2.4 的 bug 就会复发）。

### 实验二：「成稿」标签的结束态

1. 打开 `server/pipeline.py`，删掉这一行：
   ```python
   yield {"stage": "render", "status": "ok", "text": "报告已生成"}
   ```
2. 跑 `python3 server/test_ui_contract.py` → **期望 `FAIL: test_each_stage_has_terminal_state`**；
3. 加回去 → **期望 `OK`**。

这条守的是「界面标签永远转圈」那类静默 bug。

### 实验三：媒体层的 wav 重编码

1. 打开 `server/media.py`，把 `split_audio()` 里的
   ```python
   codec = ["-c:a", "pcm_s16le"] if fmt == "wav" else ["-c", "copy"]
   ```
   改成 `codec = ["-c", "copy"]`；
2. 跑 `python3 server/test_media.py` → **期望 FAIL，报 `'pcm_s16le' not found`**；
3. 改回去 → **期望 `OK`**。

---

## 八、测试覆盖不到的部分（诚实边界）

| 未覆盖 | 原因 | 现在靠什么保证 |
| :-- | :-- | :-- |
| 提示词改动后的输出质量 | 只能靠真模型，且需付费与联网 | 人工抽样读解 + 历史案例对比 |
| 真实 ASR 转写准确率 | 同上 | 人工试听 + 「需要回听的地方」一节 |
| 前端**渲染**（CSS 布局、手机端表格转卡片） | 需要浏览器 | `test_ui_contract` 只保证「事件契约」；像素级需人看 |
| 并发压测 | 只测了「额度满时回 503」与「额度归还」 | 真实压力测试需部署后做 |
| 微信/arXiv 真实抓取 | 需联网，且对方随时改版 | 每次线上失败时按 `docs/ARCHITECTURE.md` §三排查 |

`test_ui_contract` 的定位要说清：它保证**服务端发的事件前端都认识**，不保证**界面长得对**。

---

## 九、常见问题

**Q：跑 `test_app` 报「Address already in use」？**
A：它绑定 `127.0.0.1:0`（随机端口），理论上不会冲突。若报错，多半是本机禁用了 loopback 或防火墙策略，先试 `curl http://127.0.0.1:8000` 能否访问本机服务。

**Q：`test_voice` 报 `ModuleNotFoundError: No module named 'numpy'`？**
A：装 numpy。这个依赖只用于语音画像的声学分析，服务主流程不依赖它。

**Q：`test_net` 跑了 4 秒还没完，是不是卡住了？**
A：正常。它在测重试与超时翻译，含人为等待。超过 30 秒才算异常。

**Q：输出为空文件？**
A：见第五节第 2 点 —— unittest 写 stderr，重定向要带 `2>&1`。

**Q：Windows 上中文乱码？**
A：`chcp 65001` 后再跑；或在 PowerShell 里设 `[Console]::OutputEncoding = [System.Text.Encoding]::UTF8`。

---

## 十、这套测试是怎么长出来的

不是一次写成的，是每修一个 bug 补一套，六轮迭代：

| 版本 | 新增 | 起因 |
| :-- | :-- | :-- |
| v2.3 及以前 | 10 套 | 逐项功能 |
| v2.4 | `test_extract` | `extract.py` 漏 `import os`，三条抓取路一起崩，而 CI 一条都没覆盖抓取层 |
| v2.5 | `test_asr_backend`、`test_doctor` | 两个模块只在最顺路径被间接走到；补测时又抓出 `doctor.py` 直读 `exc.verify_message` 会崩 |
| v2.6 | `test_app` | `app.py` 一条测试都没有，而它是最外面那层门 |
| v2.7 | `test_media` | `test_asr` 里媒体用例在无 ffmpeg 时打一句 `[skip]` 等于占位 |
| v2.12 | `test_ui_contract` | 端到端实测发现「成稿」标签永远转圈，根因是服务端漏发结束态 |
| v2.17 | `test_pipeline_e2e` 加 12 项、`test_ui_contract` 加 4 项 | 分层展示上线：简约版每个数字须可回溯到条目；写测试时发现「未知判定被静默丢弃」导致统计自相矛盾（可核 5 条、分项只加得 4 条），补「判定不明」一档 |
| v2.19 | `test_pipeline_e2e` 加 4 项 | 简约版上线实测暴露三处缺陷：引文没回填（渲染成「-  —— 半对」）、「半对」配「无需修改」、降级半对与实质半对混算致结论过重。三条各加守门断言并做反向验证 |
| v2.13 | `test_pipeline_e2e` 加 3 项 | 另一位验证者实测「URL 残留 + 上传视频」导致报告头被污染，补上报告头的三条约束 |

共同规律：**bug 几乎都出在「没有测试守着的那个入口」**。这也是为什么这套测试值得跑第二遍、第三遍。

---

## 十一、给验证者的回报格式（建议）

若要把结果回报给维护者，用这个格式最省事：

```
环境：Python 3.12.x / numpy 已装 / ffmpeg 有（或无）
一键跑结果：PASS 16 / FAIL 0
逐条末行：与文档第四节一致 / 下列不一致：<列出>
反向实验：实验一 红→绿 ✓ / 实验二 红→绿 ✓ / 实验三 红→绿 ✓
异常：<无 / 描述>
```

---

## 十二、额外工具：输入切换端到端检查

17 套测试之外还有一支，不在 `server/test_*` 里，但同样应当跑：

```bash
python3 tools/e2e_switch_check.py --rounds 2
```

它现场生成一段 WAV、起假模型端点（含 ASR 与假公众号页）、起真 app，跑三组请求：

| 组 | 输入 | 期望 |
| :-- | :-- | :-- |
| A | 只给网址 | 报告头取自文章；**不**出现「来源构成」行 |
| B | 只给上传件 | 标题取自文件名；无作者/发布时间；类型显示「上传的音视频」 |
| C | 两者同时给 | 必须有「来源构成」行，点明正文来自音视频 |

**期望末行**：`---- 全部通过`（2 轮 × 7 项 = 14 项 `[ok]`）。需要 ffmpeg。

**为什么它不可省**：组合输入（网址 + 上传件同时存在）是单元测试天然碰不到的路径，v2.13 的 bug 就出在这里。

**注意它自带一条自检**：末尾会打印假模型的调用分布。若 `triage` 为 0，说明 `pipeline.py` 里「第一步 / 第二步 / 第三步 / 第四步」这些分步标记被改了，脚本的假模型就匹配不上 —— 此时它会主动报失败，不会假装通过。

---
