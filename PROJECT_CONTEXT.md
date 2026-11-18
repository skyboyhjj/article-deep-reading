# article-deep-reading 项目上下文

## 项目概述

**读解（Deep Read）** — 把一篇文章拆开核验的方法与服务。不是摘要器，不是评论器。

给一个链接，做四件事：判体裁、数作者序、逐条对依据、能算的地方真算一遍，然后出一份标明「确证 / 不实 / 半对 / 未核」的报告。

- 仓库：[skyboyhjj/article-deep-reading](https://github.com/skyboyhjj/article-deep-reading)（public，MIT）
- 线上部署：**暂无公开实例**，本地或 Docker 自托管
- 当前版本：**v2.10**（2026-11-18）

核心特色：五阶段流水线、五档实算判定、四道证据闸、白名单 AST 求值器、语音画像、视频号 L1 二手路径、手机窄屏适配、10 套 CI 测试。

`skills/` 与 `server/` 是**同一套方法的两副形态**：前者给平台内的助手用（纯提示词 + 脚本），后者给人用（能跑的服务）。

## 关键文件

| 文件 | 用途 |
|------|------|
| `AGENTS.md` | AI 助手指南：预读规则、实现后自检清单、已知偏差案例、技术约定 —— **动手改之前先读它** |
| `server/app.py` | HTTP 路由 + SSE 流式进度 + 上传 / 限流；命令行 `-p` / `--host` 优先于环境变量 |
| `server/pipeline.py` | 阶段编排、判定闸、报告模板（最大文件，约 72 KB） |
| `server/safe_eval.py` | 白名单 AST 求值器 —— **安全命门，任何绕过都是阻断级问题** |
| `server/extract.py` | 抓取：微信公众号 / 视频号元宝分享页 / arXiv / 通用网页；PDF 与其他二进制直接拒绝 |
| `server/asr.py` | 转录适配层：DeepSeek `/tools/asr`、OpenAI 兼容端点、本地 whisper |
| `server/media.py` | ffmpeg 抽音轨、探时长、超长切片 |
| `server/search.py` | 未核项溯源（DeepSeek `/tools/search`）+ 域名白名单制权威判定 |
| `server/voice.py` | 语音画像：声学侧 + 文本侧双证据 |
| `server/net.py` | **唯一出站口**：统一 CA、超时、两次重试、异常翻译 |
| `server/doctor.py` | 环境体检：Python、certifi、代理变量、对上游的 TLS 握手 |
| `server/llm.py` | OpenAI 兼容客户端 |
| `server/test_*.py` | 15 套测试，见下文「测试」 |
| `web/index.html` | 单页前端（无外部依赖）：Markdown 渲染、窄屏表格转卡片、原文⇄可读版切换 |
| `skills/article-deep-reading/` | 读解 SOP：`SKILL.md` + `references/checklists.md` + `scripts/`（`extract_wx.py` / `make_appendix.py` / `selfcheck_md.py`） |
| `skills/math-doc-rigor/` | 数学文档严谨化：六查渲染体检 + 三层标注 + md→PDF 保真流水线 |
| `docs/SOP.md` | 方法本体：七步流程、病灶库、实战档案、工程坑（340 行） |
| `docs/ARCHITECTURE.md` | 架构决策与失败模式：每个决策后跟一句「不这么做会怎样」（244 行） |
| `examples/` | 两份完整范例（SwarmEvolve、群作用与计数）：读解正文 + 算据附录 + 可独立跑的脚本 |
| `CHANGELOG.md` | 只记影响使用者的改动；对外发布自 v2.3 起算 |
| `requirements.txt` | **只有一个依赖**：`requests>=2.31`（服务本身零框架） |
| `Dockerfile` | python:3.12-slim + ffmpeg |
| `.github/workflows/ci.yml` | 推 main / PR 触发，10 套测试串跑 |
| `.env.example` | 环境变量全集（**项目不自动读 .env**，须 shell 导出或 docker -e） |

## 运行与部署命令

### 本地（开发 / 主用）

```bash
git clone https://github.com/skyboyhjj/article-deep-reading.git
cd article-deep-reading
pip install -r requirements.txt

export LLM_API_KEY=sk-...
python3 server/app.py                 # 默认 8000
python3 server/app.py -p 8123         # 指定端口
python3 server/app.py -p 8123 --host 127.0.0.1   # 只允许本机
```

Windows PowerShell：

```powershell
$env:LLM_API_KEY="sk-..."
python server/app.py -p 8123
```

### Docker

```bash
docker build -t deep-read .
docker run -p 8000:8000 -e LLM_API_KEY=sk-... deep-read
```

### 反向代理（生产必须）

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_buffering off;          # 关键：否则 SSE 事件被攒着一次性吐出
    proxy_read_timeout 300s;
}
```

### 环境异常先体检

```bash
python3 server/doctor.py
```

打印 Python 版本、certifi 路径与证书数、代理相关环境变量（含密码自动遮蔽）、对 `api.deepseek.com` 的实际 TLS 握手结果，并给出对应出路。

## 服务架构

单进程、零框架、标准库 HTTP 服务器：

```
                     浏览器 (web/index.html)
                              │  POST /api/analyze  （SSE 流式进度）
                              │  POST /api/upload   （音视频，走 upload_id 引用）
                              ▼
                    server/app.py  ThreadingHTTPServer
                              │
                              ▼
        ┌─────────────── pipeline.analyze() 五阶段 ───────────────┐
        │ 1 抓取 extract.fetch   微信 / 视频号 / arXiv / 通用网页  │
        │ 2 定性 stage_triage    → 可核项清单（不是摘要）          │
        │ 3 核验 stage_verify    → 四值判定 + enforce_evidence 四闸 │
        │ 3.5 溯源 stage_trace   → 只补「未核」项（可关）          │
        │ 4 实算 stage_compute   → safe_eval 真算，五档状态        │
        │ 5 成稿 stage_commentary + render_report（数字不进模型手）│
        └────────────────────────────────────────────────────────┘
                              │
                              ▼
                  上游（一个 OpenAI 兼容端点 + 三个工具端点）
                   chat · tools/asr · tools/fetch · tools/search
```

证据先于文字：前四步是取证，第五步只是排版。

## API 架构

### 本服务暴露的端点

| 端点 | 方法 | 说明 |
|---|---|---|
| `/` | GET | 单页前端 |
| `/api/health` | GET | 只返回「是否已配置」，**不暴露任何 key** |
| `/api/analyze` | POST | 主入口，SSE 推送五阶段进度，body `{url, text, title, upload_id}` |
| `/api/upload` | POST | 音视频上传，返回 `upload_id` 供 `/api/analyze` 引用 |

### 上游依赖（DeepSeek 为例，均可换 OpenAI 兼容端点）

| 用途 | 端点 | 硬约束 |
|---|---|---|
| 定性 / 核验 / 实算 / 评述 | `{base}/chat/completions` | 单次读解固定 4 次调用，正文截断 24000 字 |
| 转录 | `{base}/v1/tools/asr` | 只收 16-bit PCM WAV / Ogg Opus；≤120 秒；≤8 MiB；**无 model/language/prompt 参数**；不返回时间戳 |
| 抓取兜底 | `{base}/v1/tools/fetch` | 返回 markdown，丢作者序与发布时间 |
| 溯源 | `{base}/v1/tools/search` | 返回 `authority` 0–4、`rerank_score` 0–3；刻意不用 `need_content` |

安全约定：`LLM_API_KEY` 只在服务端环境变量里，前端永不接触；限流在内存中（`RATE_LIMIT_PER_HOUR`，默认 10，重启归零）。

## 判定规则（改报告逻辑前必读）

报告里每个数字都由 `render_report()` 从 `calc["items"]` 直接格式化写入，模型只写不含量值的评述。

### 实算五档

| 状态 | 含义 |
|---|---|
| 已实算 | 求值器跑出，且与原文声称值相差 ≤ 1% |
| 不符 | 算得出，但与声称值差超过 1% —— 这一档比「已实算」更有信息量 |
| 待比对 | 算得出，但声称值是中文表述（如「每秒钟打5万次」）无法自动解析，两数并排供人对照 |
| 仅供参考 | 算式无运算、全为常量，或原文没给声称值 |
| 未实算 | 表达式被安全规则拒绝，或算不出来 —— 按未验证对待 |

**只有前两档构成验证。** 三条入档条件：表达式必须含变量、必须给出原文声称值、实算与声称差 ≤ 1%。

### 四道证据闸（`enforce_evidence()`）

1. 用正文证明正文 → 降「未核」
2. 依据援引的全是媒体 / 门户 → 降「半对」
3. 依据援引了检索结果里没有的域名 → 降「半对」（读者点不开，可能是模型自带知识）
4. 来源全非一手 → 降「半对」

一手与否走**域名白名单**，不看搜索接口的 `authority` 分 —— 那个分把相关性也算进去了，公众号能拿 3 分。名单外一律算「相关」，白名单覆盖不到的一手站点靠二次检索 `site:` 直检补进来。

### 求值器白名单

允许：`+ - * / ** %`、一元正负、`sqrt/log/exp/abs/min/max/round/sin/cos` 等。
拒绝：`Attribute`、`Subscript`、`Lambda`、`Comprehension`、`JoinedStr`、`NamedExpr`。
上限：幂指数绝对值 ≤ 64，节点数 ≤ 120，结果须为有限实数。

### 改动后的强制复查流程

改动 `server/pipeline.py` 的任何判定逻辑后，**必须**按序执行：

1. `python server/test_replay_v10.py --assert` —— 6 组历史误判回放，v1.0–v1.6 出过的错不许回来
2. `python server/test_audit.py` —— 空表 / 假算 / 三态判定回归
3. `python server/test_safe_eval.py` —— 求值器 35 项，绕过即阻断
4. 推上去看 CI 全绿（15 套一次跑完）

改抓取层（`extract.py` 的 `fetch()` 或其分支）后，**必须**跑 `python server/test_extract.py`：
它是唯一真正调用 `fetch()` 的测试，v2.4 那个「忘 `import os` 导致三条抓取路一起崩」的 bug 就是靠它兜住的。

改动 `skills/` 下的脚本后，另需用 `examples/` 那份成品做一次端到端复跑，确认附录逐字节自校验仍通过。

## 测试

CI 跑 15 套，全部不花钱、不联网（`test_asr` 缺 ffmpeg 会自行跳过；`test_media` 的分支层不依赖 ffmpeg，真实层才 skip）：

| 测试 | 覆盖 |
|---|---|
| `test_safe_eval.py` | 求值器 35 项：12 正确 + 18 攻击 + 5 失败 |
| `test_app.py` | HTTP 层 30 项：真起服务打真请求 —— 路由、health 不泄 key、analyze 八条校验、超限后同连接不被污染、SSE 形状与临时件清理、上传六种情形、限流窗口、并发额度（503 与归还） |
| `test_extract.py` | 抓取层 11 项：`FETCH_BACKEND` 三档与未设置、通用网页、公众号 `pre` 不粘连、公式还原、UTC+8、回退 DeepSeek |
| `test_media.py` | 媒体层 34 项：假 ffmpeg 覆盖全部失败分支与命令形状（无 ffmpeg、切片失败、时长 `N/A`、产出过小），真 ffmpeg 用例另设 skip |
| `test_asr_backend.py` | ASR 适配层 29 项：四档后端判定、`describe` / `audio_spec` / `_api_root`、413·429·5xx·非 JSON·连接异常、`verbose_json` 降级重试、本地 whisper 三种失败、分片偏移三档回退 |
| `test_doctor.py` | 体检脚本 13 项：代理脱敏、`INSECURE_TLS` 告警、certifi 缺失文案、TLS 三结局、无网跑完返回 0 |
| `test_pipeline_e2e.py` | 五阶段数据流，假模型替身 |
| `test_video.py` | 视频号路径与 L1 二手标注 |
| `test_asr.py` | 转录链路（切片、偏移合并、事后校对） |
| `test_audit.py` | 自产报告审计：空表、假算、三态判定 |
| `test_search.py` | 出处检索与权威判定 |
| `test_json.py` | JSON 截断抢救与精简重试 |
| `test_net.py` | TLS 出口：CA 优先级、重试、错误翻译 |
| `test_voice.py` | 语音画像：声学 + 文本双证据 |
| `test_replay_v10.py --assert` | 6 组历史误判回放 |

## 分支管理

`main` 单分支开发，无 master/main 拆分。CI 只在推 `main` 与 PR 时触发。

## 版本控制约定

每次提交前：

1. 读取本文件，与仓库实际状态比较
2. 如有冲突（文件缺失、端点变更、判定档位调整），先改本文件再提交
3. 提交后更新「已完成 / 待办」两节

其他约定：

- 改了影响使用者的行为 → 同步 `CHANGELOG.md`（只记使用者能感觉到的改动，不记内部重构）
- 改了判定规则 → 同步 `docs/ARCHITECTURE.md` 对应决策条目（要写「不这么做会怎样」）
- 改了端点或环境变量 → 同步 `README.md` 的环境变量表与 `.env.example`
- 提交信息用中文，一句话说清改了什么
- `.gitattributes` 已固定换行符，别让 Windows 的 CRLF 污染 diff

## 待办 / 已完成 工作流

- 新任务 → 先加入 **待办** 列表
- 完成 → 移入 **已完成**，标注编号、根因 / 修复 / 文件
- 更新本文件时检查待办中是否有已完成项，一体移动

## 已完成

### 1. 五阶段流水线与安全求值（v1.x 内部迭代）

- **实现**：抓取 → 定性 → 核验 → 实算 → 成稿；模型生成的表达式一律经白名单 AST 求值
- **文件**：`server/pipeline.py`、`server/safe_eval.py`、`server/extract.py`

### 2. 五档实算判定（v1.x）

- **根因**：第一版把「模型给出算式 + 求值成功」当成实算，实测四条所谓实算全是常量，等于把原文数字换个写法再写一遍
- **修复**：三条件入档（含变量 / 有声称值 / 差 ≤ 1%），状态从两档扩到五档
- **文件**：`server/pipeline.py`（`stage_compute`、`_as_number`、`_claim_usable`）

### 3. 四道证据闸（v1.6–v2.0）

- **根因**：报告出现过「用正文证明正文」、「据 sec.gov」而检索结果里根本没有 sec.gov
- **修复**：`enforce_evidence()` 四道闸；一手判定改域名白名单制
- **文件**：`server/pipeline.py`、`server/search.py`

### 4. 未核项自动溯源（v1.8–v1.9）

- **实现**：只查「未核」项（默认 3 项）；低质量结果不许单独支撑「确证」；全非权威时按官方域名 `site:` 补检一次
- **文件**：`server/search.py`、`server/pipeline.py`（`stage_trace`）

### 5. 视频号 L1 路径（v1.x）

- **实现**：微信 UA 抓元宝分享页，取主体 / 时长 / 文案 / 元宝总结；报告强制标「L1 二手，非视频原话」
- **限制**：无视频直链，只有封面图；无发布时间
- **文件**：`server/extract.py`、`server/pipeline.py`（`video_extra`）

### 6. 接 DeepSeek ASR（v1.x）

- **实现**：ffmpeg 抽 16k 单声道 → 110 秒切片 → 偏移合并；配成后来源层级 L1 升 L0，报告加「元宝总结 vs 原声」一节
- **约束适配**：只收 WAV/Ogg、≤120 秒、≤8 MiB、无 prompt 参数
- **文件**：`server/asr.py`、`server/media.py`、`server/pipeline.py`（`_promote_to_l0`）

### 7. 转录事后校对（v1.x）

- **根因**：DeepSeek ASR 没有 prompt 参数，术语提示这条路不通
- **修复**：两条事后路径 —— 确定性规则替换（`ASR_FIX`，零成本）+ 模型校对（`ASR_PROOFREAD`，只修错字不许改写，缩减超 10% 不采信）
- **文件**：`server/pipeline.py`（`_apply_fix_rules`、`_proofread`）

### 8. 引用核对闸（v2.0）

- **根因**：一条「确证」的依据写「据 sec.gov」，而出处列是 `blogs.nvidia.com`
- **修复**：依据援引的域名不在本轮检索结果里 → 降「半对」
- **文件**：`server/pipeline.py`（`enforce_evidence`）

### 9. 语音画像（v2.1）

- **根因**：DeepSeek ASR 不返回语种也不返回置信度；「需要回听」一节经常空着，分不清是模型偷懒还是本来就没风险
- **修复**：声学（语速 / 音量起伏 / 基频波动 / 停顿长短离散度）+ 文本（字符集、方言标志词、自我修正）双证据，实测与推断分栏
- **同时修的 bug**：声学只分析第一片 110 秒而字数是全篇，算出过 22.5 字/秒
- **文件**：`server/voice.py`、`server/test_voice.py`

### 10. 「不实」需相反证据（v2.2）

- **根因**：判「不实」而「应改为」写「应查…再判断是否成立」，自相矛盾
- **修复**：存疑一律退回「半对」；同时修出处列域名重复、来源被检索池挤掉两处显示问题
- **文件**：`server/pipeline.py`

### 11. 「转录校对」与「需要回听」两节分工（v2.3）

- **根因**：两节都在谈错字，后者常空，空时读者易以为「查过了没问题」
- **修复**：转录校对 = 我改了什么（全量 + 风险标记）；需要回听 = 你该亲自听哪几处（只筛高风险与未采纳存疑项）。`_fix_risk()` 判两件事：改错的后果、改错的可能
- **文件**：`server/pipeline.py`（`_fix_risk`、`_risk_rank`、`_drop_self_duplicates`）

### 12. 工程化与健壮性（v1.x–v2.3）

- JSON 被 `max_tokens` 截断 → `_salvage_json()` 抢救完整前缀 + 精简重试
- 出站请求统一走 `server/net.py`，管 CA、超时、重试；配 `server/doctor.py` 体检
- CI 扩到 10 套测试（v2.4 起 11 套，见 13）、`CHANGELOG.md`、`.gitattributes`、README 徽章

### 13. 抓取层补测（v2.4）

- **根因**：`extract.py` 用了 `os.environ.get(...)`（`fetch()` 判断 `FETCH_BACKEND`、DeepSeek Fetch 取 key 与 base_url），却漏了 `import os`。任何走抓取的链接——公众号、arXiv、通用网页——都在那一行抛 `NameError`，一条都抓不到。**这是条致命路径。**
- **为什么 CI 没抓到**：`test_pipeline_e2e.py` 走「粘贴正文 + 假模型替身」，压根不经过 `extract.fetch()`。它测的是流水线下半段，上半段是空的。
- **修复**：补 `import os`；新增 `server/test_extract.py`（11 项，全程不联网），只把最底层 `_get` / `deepseek_fetch` 换成夹具，**真的调用 `fetch()`**，覆盖 `FETCH_BACKEND` 三档与未设置四种情形，另含三条抓取层历史坑回归（`</pre>` 粘段、`data-formula` 公式还原、`var ct` 的 UTC+8）
- **反向验证**：临时去掉 `import os`，该测试从 11 项全过变成 13 处报错，恢复后全过 —— 证明它抓得住
- **同类第二处**：`test_replay_v10.py` 的 `chk()` 定义在 `test_all()` 内部，演示模式（不带 `--assert`）必抛 `NameError`；已提到模块级，CI 两种入口都跑。共同点：**某个入口长期没人走，一过去就塌**
- **文件**：`server/extract.py`、`server/test_extract.py`、`server/test_replay_v10.py`、`.github/workflows/ci.yml`

### 14. ASR 与体检脚本补测（v2.5）

- **根因**：`asr.py` 与 `doctor.py` 此前只在最顺路径被间接走到 —— `test_asr.py` 测的是分片合并与报告节，`doctor.py` 一次都没被测。这与 v2.4 是同一个病：**某个模块长期没有测试，一改就塌**
- **新增**：`server/test_asr_backend.py`（29 项）覆盖四档后端判定与 auto 两条规则、`describe` / `audio_spec` / `_api_root`、DeepSeek 的 413·429·5xx·非 JSON·连接异常、openai 的 `verbose_json` 降级重试、本地 whisper 三种失败、分片偏移三档回退；`server/test_doctor.py`（13 项）覆盖代理脱敏、`INSECURE_TLS` 告警、certifi 缺失文案、TLS 三结局、`main()` 无网跑完
- **顺带修的真问题**：`doctor.py` 证书失败时直接读 `exc.verify_message`，该属性并非所有实例都有 —— 取不到就 `AttributeError`，**体检脚本自己崩掉，用户彻底没了线索**。改为取不到时退回 `str(exc)`
- **反向验证**：把兜底改回原写法，`test_doctor` 立刻变红（1 error），恢复后全绿
- **测出来的坑**：`call_args_list` 记录的是 `data` 字典引用，重试里 `pop()` 会改掉先前那条记录 —— 断言两次请求差异必须在假 post 内部当场快照
- **文件**：`server/test_asr_backend.py`、`server/test_doctor.py`、`server/doctor.py`、`.github/workflows/ci.yml`

### 15. HTTP 层补测（v2.6）

- **根因**：`app.py` 一条测试都没有，而它是最外面那层门 —— 路由、上传校验、正文上限、限流、SSE 收尾全靠手工点
- **做法**：绑定 `127.0.0.1:0` 起真 `ThreadingHTTPServer`，用 `http.client` 打真请求；`/api/analyze` 成功路径用假 `pipeline.analyze`，只验 SSE 形状与收尾
- **覆盖 24 项**：GET 四项（含 health 九字段且不泄露 key）、analyze 八条校验、SSE 四项（含临时上传件用完即删）、upload 六项（含文件名穿越净化）、限流窗口越过一小时
- **发现的文案偏差**：正文上限原写「20 万字符」，但请求体另有 26 万字节闸；中文 20 万字符 = 60 万字节，先撞后者，**实际可粘贴约 8.6 万汉字**。v2.8 已改准（见第 17 条）
- **发现的时序假设**：临时上传件的删除断言约 5% 概率失败 —— 客户端读到 SSE 结束标记时，服务端 `finally` 里的 `unlink` 可能还没执行。改为轮询等待（`wait_until`），连跑 20 次全过。**「SSE 结束」不等于「临时文件已清」**
- **反向验证**：临时关掉扩展名白名单，`test_rejects_bad_extension` 立刻变红（`200 != 400`），恢复后全绿
- **文件**：`server/test_app.py`、`.github/workflows/ci.yml`

### 16. 媒体层补测（v2.7）

- **根因**：`test_asr.py` 里那段媒体用例在没有 ffmpeg 时打一句 `[skip]` 就过去了 —— **那不是测试，是占位**。而真实部署最常见的情形恰恰是没装 ffmpeg
- **做法分两层**：分支层把 `media.FFMPEG` / `FFPROBE` 换成假值 + 假 `subprocess.run`，不依赖本机环境覆盖全部失败路径与命令形状；真实层用 `skipUnless(has_ffmpeg)` 跑真抽取，验证 wav 能探时长、切片能出多片
- **覆盖 34 项**：`safe_filename` 七项（含中文保留、Windows 反斜杠不识别）、`probe_duration` 六种失败均不抛、`extract_audio` 命令三段必在与「< 1024 字节视为失败」、`split_audio` 的 wav 必须重编码、`prepare` 时长探不到时不许切片
- **反向验证两处**：wav 切片改回 `copy` → 红；去掉「< 1024 字节」检查 → 红
- **文件**：`server/test_media.py`、`.github/workflows/ci.yml`

### 17. 请求体上限文案改准 + keep-alive 残留修复（v2.8）

- **根因**：文案写「粘贴正文上限 20 万字符」，但请求体另有 260 KB 字节闸；中文一字 3 字节，20 万字符 = 60 万字节，**永远先撞第一道**，而第一道的文案「请求体缺失或过大」对用户没有指导意义
- **修复**：拆成三条 —— 「请求体缺失」/「请求体过大（上限 254 KB，中文约 8.6 万汉字）；长文请分段粘贴」/「粘贴正文过长（上限 20 万字符，仅 ASCII 可达）」
- **顺带修的真问题**：超限时原来不读完请求体就回复，keep-alive 下残留字节会被当成下一个请求解析，客户端也可能先吃到连接重置而看不到提示。现在常规超限（≤5 MB）先 `_drain` 读完再回，超过 5 MB 直接回
- **新增用例**：`test_oversize_does_not_poison_next_request` —— 同一连接先超限再正常请求，第二个必须仍 200；去掉 drain 立刻红
- **文件**：`server/app.py`、`server/test_app.py`

### 18. 并发额度闸（v2.9）

- **根因**：`ThreadingHTTPServer` 线程无上限，一次读解跑 1–2 分钟，几个用户同时提交就能把线程堆起来（`docs/ARCHITECTURE.md` §三末标为「上线前必须改」）
- **实现**：`Semaphore(MAX_CONCURRENT=8)` + `SLOT_WAIT=20`；`/api/analyze` 与 `/api/upload` 先占额度，等不到回 503 并主动断开；`/api/health` 不占额度并新增 `max_concurrent` 字段
- **三个取舍**：不做无上限排队（否则线程卡在信号量上等于换地方堆）；用 `BoundedSemaphore`（漏配对当场抛错）；health 不占额度（忙时更需要能查到忙成什么样）
- **反向验证**：删掉 analyze 的 release，三条用例同时变红
- **未做**：换多进程服务器 —— 信号量只能挡「并发任务」，挡不住「并发连接」
- **文件**：`server/app.py`、`server/test_app.py`、`.env.example`、`README.md`、`docs/ARCHITECTURE.md`

### 19. README 补真实例子，examples 扩到两份（v2.10）

- **根因**：`examples/` 原只有 SwarmEvolve 一篇，那篇 20 条可核项 19 条都对，读起来像「核了一遍没什么问题」，展示不出方法能干什么
- **新增**：《群作用与计数》三件套（读解 md / 附录 md / `verify_group_action.py`）—— 十处数值九对一错，错的那处可手算复现
- **核验**：附录声明的源文件 md5 与脚本实际 md5 一致（`8db25676…`）；`examples/` 下直接 `python3 verify_group_action.py` 可跑通，输出与文档结论吻合
- **README**：新增「一个真实例子」一节，含手算表（旋转 6 类 84 / 过对顶点轴 3×16 / 过对边中点轴 3×8，合计 13；按文中错法得 11）
- **文件**：`README.md`、`examples/`（新增 3 个文件）

## 待办

- **换多进程服务器**：并发任务已由 `Semaphore(MAX_CONCURRENT=8)` 挡住（v2.9），但信号量挡不住「并发连接」本身 —— 单进程仍是单点，公开部署前换 gunicorn/uvicorn（`docs/ARCHITECTURE.md` §三末）
- **公开演示站**：需 Redis 限流（内存计数换 IP 即绕）、调低 `RATE_LIMIT_PER_HOUR`、服务端兜底 key
- **M2 任务队列 + 结果持久化**：让读解结果有可分享的链接（需一台有 Redis 的机器）
- **M3 核验原语扩充**：给 `safe_eval` 加 `det2` / `trace` / `frobenius_norm` / `pow_sum` 等固定原语，模型只能填参数不能写代码（路线 A，不引沙箱）
- **`PROOFREAD_PROMPT` 收紧**：要求 `why` 里必须给上下文证据；若「需要回听」普遍偏软，说明判据还要再硬
- **同一篇缓存**：URL + 正文哈希为键、24h，重复请求近零成本（`docs/ARCHITECTURE.md` §五降本第一步）

## 新对话快速启动

在新对话中引用此文件即可继续开发：

> 「参考 PROJECT_CONTEXT.md 继续开发 article-deep-reading」

配套必读（按任务类型）：

| 任务类型 | 先读 |
|---|---|
| 任何代码改动 | `AGENTS.md`（预读规则 + 自检清单 + 已知偏差） |
| 改方法 / 改提示词 / 写读解 | `docs/SOP.md`（七步 + 病灶库） |
| 改架构 / 改判定 / 调成本 | `docs/ARCHITECTURE.md`（决策与失败模式） |
| 在平台内用读解能力 | `skills/article-deep-reading/SKILL.md` |
| 写数学 / 理论物理类文档 | `skills/math-doc-rigor/SKILL.md` |
