# 设计 · DeepRead 错误体系 v2.1

对象：`skyboyhjj/article-deep-reading`（`main` / **v2.33** / 2026-11-24）
上一版：本文件 v2（同日）；再上一版《设计_DeepRead错误体系_融合版_20261124.md》（下称 v1，基于 v2.26）
本版来源：v1 + dscm 横展开（同日）+ 技术评审反馈的核验 + 第二轮实施层反馈（2026-11-24 17:38）
状态：**P0-a / P0-b / P1 / P2 / P3 全部落地**（v2.28 / v2.29 / v2.30 / v2.32 / v2.31），并按线上验收结论修到 v2.33；落地处的实际做法与偏差都回填在对应章节，不另起文档。
目标：异常信息「用户看得懂、报障对得上、改文案不牵连测试、外部契约变了能先知道」

本文档讲**为什么这么设计、落地时改了什么**；`docs/ERROR_CODES.md` 是同一件事的**人读码表**（25 个码一码一段）。两份互为参照：设计稿解释取舍，码表回答「这个码什么时候出现、要不要看日志」。

---

## 〇之一、v2.1 相对 v2 改了什么

增量为下表六条（原四句，v2.29 与 v2.30 落地后又补两处），全部是实施细节；不动的部分：25 码、两态出口、责任分级。

| # | 改动 | 为什么 |
| --: | :-- | :-- |
| 1 | 3.2 节补「`_exceeded` 改造」一段：`Retry-After` 与 `retry_in_seconds` 同时给，headers 透传 | v2 全篇没提 `Retry-After`，改造时容易把它当 body 字段一起吞掉 |
| 2 | 迁移路线 P0-b 与 P1 各加一条：补 `Retry-After` 头断言 | 实测 `test_app` 里 `Retry-After` 出现 0 次——丢了没人发现 |
| 3 | 新增「文档体例」一节 | v2 重构时丢了 v1 的 3.6，导致「同 condition 的两条码被并成一行」的风险 |
| 4 | 迁移路线 P3 加注：外部依赖码的用例成本与做法 | 18 处 `assertRaises` 已覆盖大半，但 4001/4002/5002 需新写，别在 5002 上造真实磁盘故障 |
| 5 | 3.2.1 的改造示例补 `minutes=max(1, round(sec / 60))`，并加「分钟与秒是两条腿」一段 | 码表 DR-3001 的 hint 是「约 `{minutes}` 分钟后恢复」，而原示例只传 `retry_in_seconds`——照抄会在第一次限流时抛 `KeyError`。同时把「缺 key 崩、多传 key 静默」做成 AST 静态守门（`test_codes` 第六节） |
| 6 | P1 落地回填（v2.30）：4.1 错误体从八字段扩到十二字段；4.2 帧与流前 JSON 同源；4.3 与第八节标「已落地」；第十二节守门从五条扩到八条（P1 长两条、P2 收尾加一条）；验收标准标达成 | 分档改由服务端 `codes.present()` 出，前端只画——原设计把判断留在前端，25 个码的口径只能靠眼睛验 |
| 7 | P3 落地回填（v2.31）：`docs/ERROR_CODES.md` 已写、守门一至五已接入 CI；第十二节补「守门三只认断言语境」；验收标准 4/5/6/10 标达成 | 守门一跑就发现两处真问题：DR-4005 是死码、DR-1004 的 scope 标错——不写守门，这两处会一直躺着 |
| 8 | P2 收尾（v2.32）：异常摘要改走帧字段 `log_exc`，源码里不再拼 `{exc}`；第十二节加守门八 | 原方案靠「app 覆盖帧 text」兜底，只有一层防线；报告头的 `notes` 根本没人覆盖，那条是真泄漏 |

---

## 〇、v2 相对 v1 改了什么

五处，全部来自评审反馈与核验结论。逐条标出来源，便于复核。

| # | 改动 | 来源 | 影响面 |
| --: | :-- | :-- | :-- |
| 1 | 新增「两态出口」一节；码表加 `stage_scope` 字段 | 反馈风险 2 + 核验要害二 | 体系结构、码表（8 列 → 9 列） |
| 2 | 构造器改为在 `_json` 内生成 payload，`write_body` 由调用方透传；P0 顺序把构造器放最后 | 反馈风险 1（v2.27 新增 `write_body`） | 落地位置、迁移路线 |
| 3 | P0 交付物加两项：11 个异常类逐个定去处、现有错误路径 → 码对照表 | 反馈风险 3 + 核验要害三 | 新增两节 |
| 4 | 守门二扫描覆盖 `write_body=` 形态与 `_route_get` 内调用，并同时扫 `status: fail` 帧 | 反馈风险 4 + 核验要害二 | 守门实现 |
| 5 | 两处表述更正：异常类「11 个（根 8 + 子 3）」；检索失败「捕获 636/661、标注 684」 | 核验 §2.5 | 文字 |

另有一处**由清单倒逼的结构改动**：v1 把「转写未配置」塞在 DR-4004 里，逐个异常定去处时发现它跟「转写失败」的责任方不同（未配置是我们的锅、失败可能是文件问题），故单列 DR-4006。**码数 24 → 25。**

---

## 一、五条设计原则

**原则一：码稳定，文案自由。** v2.27 现状是 24 处裸中文文案，`test_app.py` 的 60 个用例里有一批把文案当契约。码与文案分家，测试断言码。

**原则二：分类与文案分离。** 来自 dscm：它的 `errors.ts` 注释写着「各调用方按自己的场景给同一种情况配不同文案，所以这里只归类，不生成文案」。同一种情况在不同场景措辞不同，码表若写死文案，会逼出「一个 condition 多个码」的膨胀。

**原则三：异常自己知道该报什么。** dscm 的 `BackupError(readonly key: TranslationKey)` 比裸 `raise RuntimeError("...")` 强。DeepRead 有 11 个异常类，没有一个携带码——每次在边界重新判断类型、重新拼文案，漏一处就透出 `KeyError: 'claims'`。

**原则四：错误与降级必须分家。** 读解流程里「检索失败」「未核」「仅供参考」是降级，流程继续、报告照出；「模型没配」是错误，流程中止。混在一个体系里，后续所有「这算不算故障」的判断都会歪。

**原则五：凡是「两处要一致」的东西，必须有机器检查。** 来自 dscm 最痛的教训：它用六条正则匹配 DeepSeek 的英文出文，零测试，对方改一个词就静默失效。DeepRead 的等价物是「码表 ↔ 文档」「码 ↔ 触发用例」这两组对应关系——人为维护必然漂移。

---

## 二、体系三层

```
          ┌──────────────────────────────────────────────┐
  第一层  │ codes.py：一张表，唯一事实来源                 │
          │ code → (condition, stage_scope, http, 文案,    │
          │         hint, retryable, responsibility, log)  │
          └────────────────┬─────────────────────────────┘
                           │ 构造 payload（不负责发）
          ┌────────────────▼─────────────────────────────┐
  第二层  │ 异常持码：11 个异常类逐个定去处                │
          │ 7 个出码 · 4 个进降级 · 0 个仅内部             │
          └────────────────┬─────────────────────────────┘
                           │ 抛出
          ┌────────────────▼─────────────────────────────┐
  第三层  │ 两态出口 + 日志                                │
          │ 流前：HTTP 4xx/5xx + JSON（write_body 透传）   │
          │ 流中：HTTP 200 + SSE status=fail 帧            │
          │ 永远：结构化日志（内部详情只在这里）            │
          └──────────────────────────────────────────────┘
```

v1 把第三层写成「三个出口」，含糊。v2 分开：**前两个是「态」，第三个是「旁路」**——日志不是出口，是每一条出口都顺手写一份的东西。

---

## 三、两态出口与 v2.27 的叠加点

这一节是 v2 新增的，也是落地时最容易出错的地方。

### 3.1 为什么必须分两态

读解是一条 SSE 长流。流一旦开始（`self._sse_open()` 已发出 200 与响应头），后面无论发生什么，HTTP 状态码都改不了了——只能往流里写一个 `status: fail` 的帧。

| 态 | 触发时机 | 载体 | 字段 | 判别方法 |
| :-- | :-- | :-- | :-- | :-- |
| 流前 | 参数校验、限流、并发、上传 | HTTP 4xx/5xx + JSON body | `code/condition/error/hint/retryable/request_id` | 看 HTTP 状态码 |
| 流中 | 抓取、模型、转写、渲染 | HTTP 200 + SSE 帧 | 帧内 `stage/status/code/text/request_id` | 状态码恒 200，看帧 |

**注意**：V1 的审阅反馈里把它写成「HTTP 200 + ok:false」。SSE 帧里没有 `ok` 字段（实测 v2.27 的帧只有 `stage/status/text`），照抄会写出不存在的字段。流的形态是 `status: fail`，不是 `ok: false`。

### 3.2 与 v2.27 的 `write_body` 叠加

v2.27 给两个函数都加了 `write_body`：

```python
def _send(self, code, body, ctype, headers=None, write_body: bool = True) -> None:
def _json(self, code, obj, headers=None, write_body: bool = True) -> None:
```

`do_HEAD` 走 `self._route_get(write_body=False)`，而 `_route_get` 内部的错误返回也带 `write_body=write_body` 透传（例如 362 行的「前端文件缺失」）。

**结论：不能新建一个平级的 `error_response()` 包装 `_json`。** 那样默认带 body，`do_HEAD` 路径立刻坏，刚修好的分享卡片探测又会 501。

正确落点：

```python
# codes.py —— 只产 payload，不发送
def payload(code: str, **fmt) -> dict: ...

# app.py —— 在 _json 之上构造，write_body 原样透传
def _fail(self, code: str, headers=None, write_body: bool = True, **fmt) -> None:
    self._json(CODES[code].http, payload(code, **fmt), headers, write_body=write_body)
```

`_fail` 的 `write_body` 默认 `True`，但**每一个从 `_route_get` 调用的地方都必须显式传**——这与现有 `_json(..., write_body=write_body)` 的写法保持一致。

### 3.2.1 `_exceeded` 的改造（v2.1 新增）

现有 `app.py:182` 的 `_exceeded` 是一条专用 429 路径，它比普通错误多带三样东西，迁移时样样不能丢：

```python
def _exceeded(self, retry_after: float | None) -> None:
    sec = int(round(retry_after)) if retry_after else 0
    self._json(429, {"error": f"每小时限 {LIMITER.current_limit()} 次，请稍后再试",
                     "retry_in_seconds": sec},
               headers={"Retry-After": str(sec)})
```

改造后（v2.29 实际落地版本）：

```python
def _exceeded(self, retry_after: float | None) -> None:
    sec = int(round(retry_after)) if retry_after else 0
    self._fail("DR-3001", headers={"Retry-After": str(sec)},
               retry_in_seconds=sec, limit=LIMITER.current_limit(),
               minutes=max(1, round(sec / 60)))
```

四样都必须保留：

| 东西 | 载体 | 丢了会怎样 |
| :-- | :-- | :-- |
| `Retry-After` 头 | HTTP header | 非 JS 客户端（脚本、curl、第三方集成）拿不到等待时间 |
| `retry_in_seconds` | body 字段 | 前端倒计时读不到 |
| 「每小时限 N 次」 | 文案模板的 `{limit}` | 文案变成死数，`RATE_LIMIT_PER_HOUR` 改了不生效 |
| 「约 N 分钟」 | 文案模板的 `{minutes}` | 缺这个 key，`str.format` 直接抛 `KeyError`，整个响应崩掉 |

**分钟与秒是两条腿，不能互换。** 机器读的用秒（`retry_in_seconds`、`Retry-After`），给人看的用分钟（`{minutes}`）——「约 12 分钟后恢复」比「约 720 秒后恢复」像人话。两者必须由同一个 `sec` 派生，不许各自算一套，否则头里说 720 秒、正文说 5 分钟，用户不知道信哪个。

**注意**：`Retry-After` 是 header、`retry_in_seconds` 是 body，两者**不是同一件事的两种写法**，必须同时给。v2 的设计只在码表写了 `retry_in_seconds`，没提 header——这是 v2.1 要补的第一处。

配套（已落地，非待办）：

- `test_app` 的 429 用例断言 `retry_in_seconds` 与 `Retry-After` 头的数值一致；
- `test_codes` 第六节用 AST 静态扫 `app.py` / `pipeline.py` 里 `_fail("DR-xxxx", …)` 的实参，与码表模板里的占位符双向比对：**缺 key**（会崩）与**多传 key**（被 `str.format` 静默忽略，文案退化成没数字的死句）两种都报错。反向验证过——把 `minutes=` 删掉，该项立即变红；
- 反向验证同样证明了它为什么必须存在：若照 v2.1 早期示例（只传 `retry_in_seconds`）实现，DR-3001 会在第一次触发限流时抛 `KeyError`，而这行代码在常规测试里根本走不到。

### 3.3 `stage_scope` 字段

码表新增一列，回答「这个码可能出现在哪一态」：

| 取值 | 含义 |
| :-- | :-- |
| `pre` | 只可能在流前（参数、限流、上传类） |
| `in` | 只可能在流中（抓取、模型、转写、渲染） |
| `both` | 两态都可能（如「内部异常」在任何位置都可能抛；「模型未配置」现在在 triage 抛属流中，若改为启动即检查则属流前） |

守门脚本据此校验：标 `pre` 的码若出现在 `_sse` 调用里，报红；标 `in` 的码若出现在 `_json(4xx|5xx` 里，报红。

---

## 四、数据模型

### 4.1 流前 HTTP 返回（向后兼容）

```json
{
  "ok": false,
  "code": "DR-1005",
  "condition": "textTooShort",
  "error": "正文太短（不足 200 字）",
  "hint": "粘贴完整内容，或改用链接 / 上传音视频",
  "retryable": false,
  "retry_in_seconds": null,
  "request_id": "8f3ac21b",
  "responsibility": "user",
  "level": "fail",
  "show_code": false,
  "show_diag": false
}
```

`error` 字段名与语义不动，只加新字段。老前端不改也能跑。

后四个字段是 P1 落地时补的（原设计写八字段）。补的理由：分级若放在前端算，「25 个码各是什么样」这件事就活在浏览器的一份隐式逻辑里，只能用眼睛验；放到 `codes.present()` 里，Python 能穷举断言，前端只负责画。`level` 只有 `fail` / `warn` 两个值，与样式表同名。

### 4.2 流中 SSE 帧

```json
{"stage": "verify", "status": "fail", "code": "DR-4002", "condition": "modelFailed",
 "error": "模型调用失败", "hint": "稍后重试（已自动重试 2 次）",
 "retryable": true, "responsibility": "third", "level": "warn",
 "show_code": true, "show_diag": true,
 "text": "模型调用失败。稍后重试（已自动重试 2 次）", "request_id": "8f3ac21b"}
```

失败帧挂在实际失败的阶段上（保留现有行为），补 `code` 与 `request_id`。首帧 `quota` 同时带 `request_id` 与 `version`，前端不必另发请求就能拼诊断信息。

**P1 落地时加了 `error` / `hint` / `level` / `show_code` / `show_diag` / `responsibility`，并改写 `text`。** 帧与流前 JSON 走的是同一个 `codes.payload()`，所以字段一字不差——否则「同一件事在两种形态下说两句话」迟早会漂。`text` 保留是给不认识新字段的老页面，内容改成「人话 + 改法」，pipeline 兜底帧里那句 `内部错误：KeyError: xxx` 因此不会再上页面。

版本号只有一个真源：`app.APP_VERSION`。`/api/health`、SSE 首帧、用户复制的诊断信息第一行都从它取；`test_ui_contract` 会把它与 `CHANGELOG` 的首个版本标题比对，发版时漏改一处就报红（反向验证过：改成 2.31 立即变红）。注释里点名的测试文件也由测试自己核对——`app.py` 曾写成不存在的 `test_web`，读者按注释去找会找不到，于是误判「这个守门没做」。

### 4.3 诊断信息

前端 `#err` 旁加一个「复制诊断信息」动作：

```
DeepRead v2.27 · 2026-11-24T17:30:11+08:00
code: DR-4002 · condition: modelFailed
stage: verify · request_id: 8f3ac21b
status: 200 · elapsed: 11.3s
ua: Chrome/131 (Windows)
```

其中 `stage` 一行是 DeepRead 比 dscm 多出来的东西——纯前端不知道流程走到哪，SSE 服务知道。

**已落地（v2.30）**：`show_code` / `show_diag` 为真的错误旁挂「复制诊断信息」，另附一个可展开的 `<pre>` 显示同一段文本（复制失败时用户能手动选）。`navigator.clipboard` 只在 https 与 localhost 可用，局域网 IP 直连是 http，所以保留了 `document.execCommand("copy")` 的退路——这条退路不是给老浏览器留的，是给「手机同 WiFi 扫 IP 访问」这条真实路径留的。

---

## 五、码表（九个字段，25 个码）

`server/codes.py` 一张表。`resp` 列：`user` 用户可改 / `third` 外部依赖 / `us` 我们的锅。`scope` 列即 `stage_scope`。

这里说的**九个字段是码表这一行自己的**（key / condition / scope / http / message / hint / retryable / responsibility / log）；线上响应体是十二个（4.1 节），两者的关系是「码表出文案与分级，调用方补 ok / request_id / retry_in_seconds」。

| code | condition | scope | http | 用户看到 | 可重试 | 责任 | 日志 |
| :-- | :-- | :-- | --: | :-- | :-- | :-- | :-- |
| DR-1001 | incompleteRequest | pre | 400 | 请求内容为空 | 否 | user | 否 |
| DR-1002 | bodyTooLarge | pre | 400 | 内容太大 | 否 | user | 否 |
| DR-1003 | malformedJson | pre | 400 | 请求格式有误 | 是 | user | 否 |
| DR-1004 | noInput | pre | 400 | 没有可读解的内容 | 否 | user | 否 |
| DR-1005 | textTooShort | pre | 400 | 正文太短（不足 200 字） | 否 | user | 否 |
| DR-1006 | textTooLong | pre | 400 | 正文过长（上限 20 万字符） | 否 | user | 否 |
| DR-1007 | notFound | pre | 404 | 接口不存在 | 否 | user | 否 |
| DR-2001 | incompleteRequest | pre | 400 | 上传中断 | 是 | user | 否 |
| DR-2002 | fileTooLarge | pre | 413 | 文件超过上限 300 MB | 否 | user | 否 |
| DR-2003 | unsupportedType | pre | 400 | 不支持的文件类型 | 否 | user | 否 |
| DR-2004 | invalidUploadId | pre | 400 | 上传标识无效 | 否 | user | 否 |
| DR-2005 | uploadExpired | pre | 404 | 文件已过期（上传超 24 小时） | 否 | user | 否 |
| DR-2006 | uploadIncomplete | pre | 400 | 上传不完整 | 是 | user | 否 |
| DR-3001 | quotaExceeded | pre | 429 | 本小时额度已用完 | 是 + 倒计时 | user | 否 |
| DR-3002 | concurrencyFull | pre | 503 | 同时读解的人较多 | 是 | user | 否 |
| DR-3003 | limiterUnavailable | pre | 503 | 服务暂时繁忙 | 是 | third | 是 |
| DR-4001 | modelNotConfigured | both | 200 | 服务端尚未配置模型 | 否 | us | 是 |
| DR-4002 | modelFailed | in | 200 | 模型调用失败 | 是 | third | 是 |
| DR-4003 | fetchFailed | in | 200 | 这个链接抓不到内容 | 否 | third | 是 |
| DR-4004 | transcribeFailed | in | 200 | 音视频转写失败 | 是 | third | 是 |
| DR-4005 | mediaFailed | in | 200 | 这个文件读不出来 | 否 | third | 是 |
| DR-4006 | transcribeNotConfigured | both | 200 | 服务端尚未配置转写 | 否 | us | 是 |
| DR-5001 | frontendMissing | pre | 500 | 页面文件缺失 | 否 | us | 是 |
| DR-5002 | storageWriteFailed | pre | 500 | 服务器存储异常 | 是 | us | 是 |
| DR-5003 | internalError | both | 200 | 服务异常 | 否 | us | 是 |

三处说明：

1. **`http` 列标 200 的六个码，实际状态码恒为 200**，只出现在流中（除 `both` 的 4001/4006/5003 可能在流前）。这是一个占位值，不是「这些错误返回 200 表示成功」。
2. **没有码的一个**：检索不可用（`searchUnavailable`）。它是降级不是错误（见第九节）。
3. **DR-4006 是 v2 新增**：`ASRNotConfigured` 与 `ASRError` 责任方不同——未配置是我们的锅（该发诊断信息），转写失败可能是文件问题（该换文件）。v1 挤在 4004 里，分级会错。

---

## 六、异常清单：11 个 class 逐个定去处

v1 写「11 个异常类」，v2 把清单落全。**这是 P0 的第一份交付物**——不先定这个，码会漏配。

| 异常类 | 父类 | 去处 | 码 / 标记 | 理由 |
| :-- | :-- | :-- | :-- | :-- |
| `LLMNotConfigured` | LLMError | 出码 | DR-4001 | 服务端没配，用户无解 |
| `LLMError` | RuntimeError | 出码 | DR-4002 | 外部模型故障，可重试 |
| `FetchError` | RuntimeError | 出码 | DR-4003 | 目标页抓不到，改用正文 |
| `ASRNotConfigured` | ASRError | 出码 | DR-4006 | 与「转写失败」责任方不同 |
| `ASRError` | RuntimeError | 出码 | DR-4004 | 转写失败，可换文件 |
| `MediaError` | RuntimeError | 出码 | DR-4005 | 文件读不出来 |
| `RateLimiterError` | RuntimeError | 出码 | DR-3003 | 限流服务不可用 |
| `SearchNotConfigured` | SearchError | 降级 | `notice: search_unavailable` | 不阻断，报告标「未溯源」 |
| `SearchError` | RuntimeError | 降级 | `notice: search_unavailable` | 同上 |
| `UnsafeExpression` | ValueError | 降级 | `notice: calc_untrusted` | 实算不可信，报告标「仅供参考」 |
| `EvalError` | ValueError | 降级 | `notice: calc_untrusted` | 同上 |

合计：**7 个出码、4 个进降级、0 个仅内部**。

（v1 与审阅反馈都用过「7 个」这个数——那是 8 个根类减去 `UnsafeExpression`，且排除了 3 个 `NotConfigured` 子类。按 7 个配码会漏掉 `LLMNotConfigured`，属于「模型未配置」和「模型调用失败」混为一谈。）

---

## 七、现有错误路径 → 码（P0 第二份交付物）

v2.27 实测：**字面量错误返回 23 处 + `_exceeded` 内部 1 处 = 24 处**；`_exceeded` 被 2 处调用，故独立触发路径 **25 条**；另有流中 `fail` 帧（v2.27 时点数记作 4 处，v2.29 实测为 **9 处**：pipeline 8 + app 兜底 1，见后文偏差一）。

按码聚合（逐行清单由 P0 的脚本生成，不入本文档）：

| code | 覆盖位置（v2.27 行号） | 字面量数 |
| :-- | :-- | --: |
| DR-1001 | 328、414（请求体缺失/过大） | 2 |
| DR-1002 | 421（请求体过大） | 1 |
| DR-1003 | 333、427（JSON 解析失败） | 2 |
| DR-1004 | 448（无输入来源） | 1 |
| DR-1005 | 456（正文太短） | 1 |
| DR-1006 | 451（正文过长） | 1 |
| DR-1007 | 370、389、406（not found） | 3 |
| DR-2001 | 265（缺 Content-Length） | 1 |
| DR-2002 | 268（413 文件过大） | 1 |
| DR-2003 | 274（扩展名不支持） | 1 |
| DR-2004 | 337、438（upload id 非法） | 2 |
| DR-2005 | 442（上传件过期） | 1 |
| DR-2006 | 307（传输不完整） | 1 |
| DR-3001 | 185（由 284、464 两处调用） | 1 |
| DR-3002 | 238（并发已满） | 1 |
| DR-3003 | 282、462（限流服务不可用） | 2 |
| DR-5001 | 362（前端文件缺失） | 1 |
| DR-5002 | 302（写入失败） | 1 |
| DR-5003 | 481 帧 + pipeline 三处兜底 | 0（4 帧） |

未被上表覆盖但必须配码的：DR-4001~4006（六种外部依赖），由异常清单直接给出，不在 `_json` 字面量里。

口径说明：上表「处数」按**字面量**计，合计 24 处，与 v2.27 实测一致。两处例外：

- DR-3001 的字面量只有 185 这一处（在 `_exceeded` 内），但被 284（上传）与 464（读解）两条路径调用，所以用户的触发路径是 2 条；
- DR-5003 的字面量是 0，表里记的几处全是流中 `fail` 帧（app 兜底 1 + pipeline 兜底，v2.29 实测 pipeline 共 8 处 fail 帧）。

因此口径统一为：**字面量 24 处**（上表合计），其中 185 这一处被两条路径调用，故**用户可触发的路径 25 条**；另有**流中 `fail` 帧 9 处**（v2.29 实测：pipeline 8 + app 兜底 1），不计入字面量。

---

## 八、前端分级：由责任驱动，不是由号段驱动

| 责任 + 可重试 | 样式 | 位置 | 附带 |
| :-- | :-- | :-- | :-- |
| user + 不可重试 | 红（`--fail`） | `#err` | 只给人话与改法，**不显示码** |
| user + 可重试（限流、并发） | 黄（`--warn`） | `#err` | 倒计时，码放末尾小字 |
| third | 黄（`--warn`） | `#err` | 换方式的建议 + 码 + 诊断信息 |
| us | 红（`--fail`） | `#err` | 诊断信息 + 一键复制 |

两点细节：

1. **输入类不显示码**。用户改一下就行，面前摆个 `DR-1005` 只会添乱。
2. **hint 支持动作**（学 dscm 的 `link`）：413 文件过大旁挂「改用粘贴正文」，上传过期旁挂「重新上传」。点一下聚焦输入框或重开文件选择。

色值不必新增：`--fail`（`#b23b3b`）与 `--warn`（`#a86a12`）都在，v2.22 校准过对比度（均到 4.96）。

**已落地（v2.30）**：分档不写在前端。`codes.present()` 出 `level` / `show_code` / `show_diag` 三个字段，前端只按 `level` 挑样式类（`.err.fail` / `.err.warn`）、按两个布尔决定摆不摆码与诊断。这样 25 个码的口径能在 `test_codes` 里穷举断言，不必进浏览器看。前端另有两道守门（`test_ui_contract`）：不许直接往 `#err` 写文本（绕过 `showErr` 会把码与诊断一起丢掉），不许出现码表里没有的码（动作按钮会点了没反应）。两档配色都复测了对比度 4.5。动作按钮表在前端（`ERR_ACTION`），因为它绑的是「聚焦输入框 / 重开文件选择」这类纯界面动作，服务端不该知道；键是码，值里没有文案。

---

## 九、错误与降级的分界

| | 错误 | 降级 |
| :-- | :-- | :-- |
| 判据 | 用户拿不到报告 | 用户拿到报告，内容打折 |
| 表现 | 流程中止，`#err` 出提示 | 流程继续，报告里标「未核」「仅供参考」「检索失败」 |
| 标识 | `code`（DR-xxxx） | `notice`（如 `search_unavailable`、`calc_untrusted`） |
| 前端 | 红/黄提示，中止 | 报告内黄字标记，不打断 |
| 统计 | 按 code 计 | 按 notice 计，单独看比例 |

一句话口径：**用户还能不能拿到报告**。能，就是降级。

v2.27 里检索失败（`pipeline.py` **捕获在 636、661，标注文案在 684**）就是降级——报告写「（检索失败：…）」但继续出稿。

---

## 十、日志与隐私

一行式 JSON，写 stdout（容器环境天然收集）：

```json
{"ts":"2026-11-24T17:30:11+08:00","rid":"8f3ac21b","code":"DR-4002",
 "http":200,"path":"/api/analyze","ms":11320,
 "ip":"sha256:1a2b…","exc":"LLMError: timeout after 60s"}
```

**更正（v2.29 落地实测）**：原示例里有个 `"stage":"verify"`，实际没实现——`_log_error` 拿不到阶段（它在 pipeline 的生成器里，异常出口在 app 层）。要补得让 pipeline 把当前阶段抛上来，成本大于收益，所以现在**日志行没有 stage，阶段只在 SSE 帧与前端诊断信息里**。排查时靠 `rid` 把日志行与用户的诊断信息对起来，两边的 `rid` 是同一个。

四条纪律：

1. **不记正文**：只记长度、体裁、阶段。
2. **不记明文 IP**：直接调 `ratelimit._hash_ip()`——现成的 `sha256(RL_SALT+ip)[:32]`，不要另造一套。
3. **不记 traceback 到响应**：`exc` 只进日志，页面只给码与追踪号。
4. **只在该写时写**：`log` 列为「否」的码（输入类、额度类）不写，否则日志被「正文太短」刷满，真正要看的 5xxx 被淹没。
5. **诊断信息只走用户的手**（v2.30）：复制出来的版本、阶段、耗时、UA 全在前端本地拼装，一次网络请求都不发；用户不点复制，它就哪儿也不去。反过来，服务端日志里也不记 UA——抓 UA 属指纹级信息，为定位一个问题不值得付这个代价。

---

## 十一、文档体例（docs/ERROR_CODES.md）

一码一段，字段固定四行：

```markdown
### DR-1005 · 正文太短

- 用户看到：正文太短（不足 200 字）· 粘贴完整内容，或改用链接 / 上传音视频
- 触发：POST /api/analyze，pasted 非空、无 url、无 upload_id，且 len(pasted) < 200
- 何时加：与 pipeline 的「≥200 字走正文」判定保持一致
- 排障：不需要看日志，检查调用方是否截断了正文
```

三条规矩：

1. **按码列行，不按 condition 合并。** DR-1001（请求体缺失）与 DR-2001（上传缺 Content-Length）共用 `incompleteRequest`，但触发路径与用户动作完全不同，必须各占一段。condition 是内部统计口径，不是文档的分节依据。
2. **「排障」行必须回答「要不要看日志」。** 码表 `log` 列为「否」的写「不需要看日志」并给出替代检查点；为「是」的写清看哪几个字段。
3. **段落标题固定为 `### DR-xxxx · 中文短名`。** 守门一按 `DR-\d{4}` 抓取，格式不一致会漏。

守门一因此有一个隐含要求：文档里出现的每个码都必须在 `codes.py` 里有对应，反之亦然。某段在码表里删了而文档没删，对称差会报红——这正是它要防的。

---

## 十二、机器守门：八条

八条全部已接入 CI：守门一至五、八在 `server/test_guards.py`，守门六在 `test_ui_contract.py`，守门七在 `test_codes.py`。每条都做过反向验证——把被守的东西改坏，对应项必红。

**守门一：码表 ↔ 文档双向一致。** 解析 `docs/ERROR_CODES.md` 里的 `DR-\d{4}`，与 `codes.py` 的键集合比对称差，少一个则红。

**守门二：源码不出现无码的错误返回，且两态全覆盖。** 扫描 `server/*.py`：

- `_json(4xx|5xx` 之后没有 `code=` 或 `_fail(` 的，报红；
- `_sse({... "status": "fail"` 之后没有 `code` 的，报红；
- 正则必须允许尾随参数，即匹配 `_json(400, {...}, headers=..., write_body=...)` 这种形态（v2.27 的 `_route_get` 内就有一处）；
- `_route_get(write_body=...)` 函数体内的所有错误返回也要扫到，不能只扫 `do_POST`。

**守门三：每个码至少一个触发用例。** 收集 `codes.py` 全部码，与测试断言过的码集合比对称差，未覆盖的码红。**落地细节（v2.31）**：只认断言语境——`code == "DR-x"`、`"code": "DR-x"`、`assertEqual/assertIn(..., "DR-x")`。第一版扫全文，结果把用例里的码改掉、只留一行注释写旧码，守门照样绿。

**守门四：文案不参与断言。** 扫描 `tests/`，出现 `assertIn("某句中文", ...["error"])` 的报红，提示改为断言 `code`。

**守门五（v2 新增）：`stage_scope` 与调用位置一致。** 标 `pre` 的码出现在 `_sse` 调用里报红；标 `in` 的码出现在 `_json(4xx|5xx` 里报红。这条防的是「把流中的错写成流前」，前端会拿不到帧。

**守门六（落地新增，已接入 CI）：前端不许绕过统一出口。** `test_ui_contract` 扫 `web/index.html`，出现 `$("err").textContent` 直接赋值就报红——绕过 `showErr` 意味着码与诊断信息一起丢掉，页面上看不出这条错误属于谁。同一组还查两件事：前端出现的每个 `DR-xxxx` 都必须在码表里（否则动作按钮点了没反应），以及两个 `level` 都只有 `fail` / `warn` 两档样式（第三种会静默落回无样式）。

**守门七（落地新增，已接入 CI）：文案模板的占位符与调用点实参双向一致。** `test_codes` 第六节用 AST 扫 `_fail("DR-xxxx", …)` 的实参，与 `message + hint` 里的占位符比对：**缺 key 会 `KeyError`（整个响应崩），多传 key 会被 `str.format` 静默忽略（文案退化成没数字的死句）**。两种都只在真实触发时暴露，常规测试走不到——所以只能静态扫。反向验证：删掉 `_exceeded` 里的 `minutes=`，该项立即变红。

**守门八（P2 收尾新增，已接入 CI）：上页面的文案不许插异常摘要。** 扫 `pipeline.py` / `app.py` 里 `text` / `message` / `hint` / `notes` 的 f-string，出现 `{exc}` 或 `{type(exc).__name__}` 即红。配套两条反向检查：pipeline 的 fail 帧必须带 `log_exc`（异常得有地方去），app 必须 `pop` 它并交给 `_log_error`（摘了不喂等于丢线索）。三项都反向验证过——帧 text 改回插 exc、notes 改回插 exc、`pop` 改回 `get`，依次变红。

为什么这条非加不可：v2.30 的做法是「app 把帧的 text 换成码表文案」，看着够用，但那是**唯一一层防线**；报告头的 `notes` 它根本覆盖不到，`页面抓取失败（HTTP 403）` 就这么一直出现在报告里。守门八守的是「源码里没有第二条上页面的路」，不靠下游兜底。

---

## 十三、迁移路线

| 阶段 | 内容 | 耗时 | 上线后可见 |
| :-- | :-- | --: | :-- |
| P0-a | `codes.py` 码表（25 码，九字段）；异常清单与路径对照表落地；`request_id` 生成与响应头；结构化日志（复用 `_hash_ip`） | 一天 | 无 |
| P0-b | 构造器 `_fail` 写在 `_json` 之上，`write_body` 透传；`_route_get` 内 4 处错误一并改；**`_exceeded` 改为 `_fail("DR-3001")` 且 `Retry-After` 头与 `retry_in_seconds` 同时给** | 一天（含 21 处断言迁移） | 无 |
| P1 | 前端四态展示；诊断信息与一键复制；hint 动作按钮；**补一条 `Retry-After` 头断言（v2.30 已补：`test_app` 断言头体一致）** | 半天 | 用户开始看到分级提示与追踪号 |
| P2 | 内部异常脱敏（481 帧的 `服务异常：{type}: {exc}` 改为码 + 追踪号）；pipeline 三处 fail 帧补码 | 半天 | **已完成（v2.29 脱敏 + v2.32 收尾）**：7 处帧与 2 处 notes 不再拼 exc，摘要走 `log_exc` 进日志 |
| P3 | `docs/ERROR_CODES.md`（按第十一节体例，一码一段不合并）；守门一至五接入 CI（六、七已接入） | 一天 | **已完成（v2.31）**：文档 25 段、守门 11 项、新增流水线错误路径用例 8 项 |

P3 的用例成本分布（排期时留意，别卡在最后一条）：

| 码组 | 现状 | 做法 |
| :-- | :-- | :-- |
| DR-4003 / 4004 / 4005 / 4006 | `test_extract` 3 处、`test_asr_backend` 6 处、`test_media` 6 处，均为 `assertRaises` | 加一行 `exc.code == "..."` 即可，属**迁断言**不是新写 |
| DR-4001 / 4002 | `test_pipeline_e2e` 的 llm 是假替身，不抛错 | 新写两条，`mock` 抛 `LLMNotConfigured` / `LLMError` |
| DR-5002 | 只有 `MAX_UPLOAD` 的 413 用例 | `mock.patch("builtins.open", side_effect=OSError)`，**不要造真实磁盘故障** |

两点顺序约束：

- **日志先于脱敏**：P2 一旦脱敏，没有日志就等于「用户有追踪号、我们没有记录」。
- **构造器后于码表**：P0-b 单独切出来，是因为它是唯一会碰 `write_body` 的改动，做完必须回归 `test_app` 全部 60 项（含 `test_head_og_image`），确认 HEAD 行为未坏。

总工期 3.5 天（比 v1 多半天，多的是 P0-b 的独立切分与回归）。

### 实施进度（2026-11-24 回填）

| 阶段 | 版本 | 状态 | 与计划的偏差 |
| :-- | :-- | :-- | :-- |
| P0-a | v2.28 | 完成 | 无 |
| P0-b | v2.29 | 完成 | 三处偏差 + 一项补测，见下 |
| P1 | v2.30 | 完成 | 一处口径变化：分档改由服务端出（`codes.present()`），前端只画不判断 |

**偏差一：pipeline 的流中 fail 帧是 8 处，不是「三处」。** 原文（本表 P2 行）写「pipeline 三处 fail 帧补码」，实测为 8 处：抓取失败、无可用输入、转写未配置、转写失败、LLM 未配置（两处）、模型调用失败、内部错误。已全部带码。

**偏差二：断言迁移只能随 P0-b 做，不能等 P3。** 计划把「21 处文案断言迁码断言」排在 P3，但 `_fail` 一上线文案就变（原来的长句被拆成 message + hint），21 处断言当场变红——不做迁移，P0-b 无法绿灯。所以 P3 的这部分提前到 P0-b 完成。

**偏差三：兜底帧脱敏也在 P0-b 做了。** 计划排在 P2，实际改 `_fail` 时顺手改了（`服务异常：KeyError: xxx` → 「服务异常 + DR-5003 + 追踪号」），否则新加的 `code` 字段与页面文案会互相矛盾。

**补测一：占位符的双向静态守门（v2.29 后，评审反馈触发）。** 评审指出码表 DR-3001 的 hint 用 `{minutes}`、而 3.2.1 示例只传 `retry_in_seconds`。实际实现已按 `minutes` 口径传参，但这类不一致靠人眼与运行测试都抓不住（缺 key 只在限流真正触发时崩，多传 key 会被 `str.format` 静默忽略）。故在 `test_codes` 增第六节：用 AST 扫 `app.py` / `pipeline.py` 里 `_fail("DR-xxxx", …)` 的实参，与模板占位符双向比对。反向验证：删 `minutes=` 实参，该项立即变红。

顺带记一笔这次踩的坑：v2.29 首次推送漏了 `server/codes.py`（DR-1002 的文案模板改在码表里），CI 报 `'8.6 万' not found in '内容太大'`。教训——推送清单要按「工作区 vs 远端逐文件比对」生成，不能凭改动记忆列。

**P1 落地（v2.30）三处与计划的出入：**

1. **分档从「前端算」改成「服务端出」。** 原计划前端按 `responsibility + retryable` 推颜色与摆不摆码。落地时发现：这么一来 25 个码的展示口径就活在浏览器里，只能靠眼睛验。改为 `codes.present()` 出 `level` / `show_code` / `show_diag`，`test_codes` 第七节可以逐码穷举断言，前端退化成「照着画」。
2. **流中帧与流前 JSON 合并到同一个 `payload()`。** 原计划只给帧补 `code` 与 `request_id`。落地时改为整份 payload 灌进帧——字段集不一致，就是在为「两种形态说两句话」留后门。连带把帧的 `text` 改写为「人话 + 改法」，pipeline 兜底帧的内部异常摘要不再上页面（这条原排在 P2）。
3. **顺手修掉一个一直存在的吞错。** 前端 `handleEvent` 里 `stage === "error"` 直接 return——服务端兜底错误帧从未显示过。它恰恰是最该被看见的一类。

---

## 十四、验收标准

1. 随便触发一个错误，页面上能看到分级样式；属 `third` 或 `us` 的，能看到码与诊断信息（v2.30 已达成：四形态见第八节）。
2. 把码发过来，能立刻说出「哪一行代码、要不要翻日志」。
3. 改一句错误文案，测试不红。
4. 新增一条错误路径但忘配码，CI 红（v2.31 达成，守门二逐帧扫）。
5. 码表加码但没写文档段落，CI 红（v2.31 达成，守门一）。
6. 加码但没写触发用例，CI 红（v2.31 达成，守门三，只认断言语境）。
7. **`HEAD /og.jpg` 仍返回 200 且无 body**（P0-b 的回归硬指标，v2.27 刚修好，不能被构造器搞坏）。
8. **流中错误必须以 200 + `status: fail` 帧出现，且帧里有 `code`**（用一次真实的模型失败或断流触发，可用测试替身）。
9. **429 响应同时带 `Retry-After` 头与 `retry_in_seconds` body，且两值一致，正文的分钟数由同一秒数派生**（v2.29 已覆盖：`test_app` 断言头体一致，`test_codes` 断言占位符实参）。
10. `docs/ERROR_CODES.md` 里 DR-1001 与 DR-2001 各占一段，没有因为共用 `incompleteRequest` 被合并（v2.1 新增；v2.31 达成，两段各四行，守门一保证既不合并也不漏段）。
11. **码表模板的占位符与调用点实参双向一致**：缺 key 会崩、多传 key 会被静默忽略，两者都在 `test_codes` 第六节的 AST 扫描里报错（v2.29 后补）。
12. **前端不许绕过统一出口**：`web/index.html` 里出现 `$("err").textContent` 直接赋值即报红；前端出现的每个码都在码表里（v2.30 达成，守门六）。

---

## 十五、从 dscm 吸收了什么、没吸收什么

| dscm 的做法 | 吸收 | 说明 |
| :-- | :-- | :-- |
| 分类与文案分离 | 是 | `condition` 列的存在正是为此 |
| 错误类携带 key | 是 | 11 个异常类逐个定去处 |
| `isRetryableApiError` | 是 | 码表里的 `retryable` + `retry_in_seconds` |
| 带操作链接的提示（`link`） | 是 | hint 旁挂动作按钮 |
| 一键复制诊断信息 | 是 | 且加上 `stage`，比 dscm 更有用 |
| 正则匹配外部文案 | 否 | 反向吸收。DeepRead 的对策：外部依赖失败统一归 4xxx，原始错误只进日志——不猜文案，靠归类 |
| i18n key 当码 | 否 | DeepRead 单语言、文案在后端，key 这层没有收益 |
| `type?: 'error'` 二态 | 部分 | 扩成四态（按责任），比二态细，比完整设计系统轻 |

**一句话**：dscm 的分层思想全部吸收，它的实现捷径一个不学。

---

## 十六、版本对照

| 项 | v1（基于 v2.26） | v2（基于 v2.27） |
| :-- | :-- | :-- |
| 码数 | 24 | **25**（新增 DR-4006 转写未配置） |
| 码表字段 | 8 | **9**（加 `stage_scope`） |
| 出口表述 | 「三个出口」 | **两态 + 日志旁路** |
| 构造器落点 | 未指定 | **`_json` 之上，`write_body` 透传** |
| 异常清单 | 「11 个异常类」概述 | **11 个逐个定去处**（7 出码 / 4 降级） |
| 路径对照 | 无 | **按码聚合，覆盖 24 处字面量 + 2 处调用 + 4 处帧** |
| 守门 | 4 条 | **5 条**（加 scope 校验） |
| 工期 | 3 天 | **3.5 天**（P0 拆 a/b） |

### v2.1 相对 v2

| 项 | v2 | v2.1 |
| :-- | :-- | :-- |
| `_exceeded` 迁移 | 未提 | **3.2.1 节：`Retry-After` 头 + `retry_in_seconds` body + 动态 `{limit}` 三样保留** |
| `Retry-After` 测试 | 无 | **P1 补一条（v2.30 已落地：`test_app` 断言头与体一致，前端真倒计时）** |
| 文档体例 | 缺（v2 重构时丢了 v1 的 3.6） | **第十一节恢复，明确「按码列行、不按 condition 合并」** |
| P3 用例成本 | 未说明 | **加分布表：18 处 `assertRaises` 迁断言；4001/4002/5002 新写，5002 用 mock** |
| 「四条机器守门」标题 | 标题四条、内容五条（不一致） | **改「五条」** |

（另修正本表上一栏「6 出码 / 5 降级」为「7 出码 / 4 降级」——v2 改第六节与结构图时漏改了这张表。）

---

## 附录 A、修改记录

本文档在 2026-11-24 一天里走了六轮：起草 v2.1 → 评审反馈修补 → v2.30 落地回填 → v2.31（P3）→ v2.32（P2 收尾）→ v2.33（线上验收修复）。改动都留在正文对应位置，旧说法不覆盖、与更正并列，这里只给一条时间轴。

| 时间 | 版本 | 改了什么 | 触发 |
| :-- | :-- | :-- | :-- |
| 18:20 | v2.1 | 起草四条：`_exceeded` 改造（`Retry-After` 头 + `retry_in_seconds` 体）、文档体例、P3 用例成本、`write_body` 透传 | 第二轮实施层反馈（三条） |
| 18:35 | v2.1 | 加第 5 条：3.2.1 示例补 `minutes=max(1, round(sec / 60))`；把「缺 key 崩 / 多传 key 静默」做成 AST 静态守门 | 评审指出 DR-3001 的 hint 用 `{minutes}`，而示例只传了秒 |
| 19:10 | v2.1 + 回填 | 十一处落地回填：头部状态行、改动表第 6 条、4.1 十二字段、4.2 帧同源、4.3 诊断落地、第八节分档前置、第十节（更正示例里假的 `stage` + 加纪律五）、第七节（帧数 4 → 9）、第十二节（守门 5 → 7）、第十三节（P1/P2/P3 状态）、第十四节（验收标达成 + 第 12 条） | P1 落地（v2.30）+ 自查发现文档与实现对不上 |
| 19:20 | — | 修 `app.py` 版本注释里的文件名（`test_web` → `test_ui_contract.TestVersion`）；新增守门：注释点名的 `test_*.py` 必须真的存在 | 评审按注释去找 `test_web.py` 没找到，误判「这个守门不存在」 |
| 19:45 | v2.31 | P3 落地：写 `docs/ERROR_CODES.md`（25 段四行）、守门一至五接入 CI、补五条流水线错误路径用例；守门逼出两个真问题（DR-4005 死码、DR-1004 scope 标错） | P3 开工 |
| 21:05 | v2.32 | P2 收尾：7 处帧 + 2 处 notes 去掉异常拼接，摘要改走 `log_exc`；第十二节加守门八（页面文案不许插 exc） | P2 尾巴 |
| 22:55 | v2.33 | 线上 v2.32 验收后的修复：失败保留上传件（`completed` 改判 done 帧）、前端体积预校验、报告小节标题分场景、README 补 `client_max_body_size 60m` | 线上验收发现 |
| 23:05 | — | 口径统一：`BODY_LIMIT` 两处注释与 `docs/ERROR_CODES.md` 的 DR-1002 触发条件改「260 000 字节（文案显示 254 KB）」；DR-2002 的「用户看到」改用 `{limit}` 占位（代码默认 300、线上配 50） | 复审「口径 5」时发现只改了定义处、漏了调用处 |
| 待 | — | 页面渲染（四形态/复制/倒计时）仍需人工确认；429 与 DR-4004 未在线上覆盖 | — |

三条约定写在这里，免得后来人「整理」时抹掉线索：

1. **旧说法不删，并列更正。** 第十节的日志示例保留了原本的 `stage` 字段，紧跟一段「实际没实现」的更正——把旧示例删干净，就看不出这里曾经写错过。
2. **数错了要留痕。** 第七节的「流中 `fail` 帧 4 处」改成 9 处，同时保留「v2.27 时点数记作 4 处」这句，读者才知道是点数方法变了还是代码变了。
3. **文件名保持 v2.1，不升 v2.2。** 设计与计划没变，变的是落地回填；一天之内生成四个版本号，读者反而不知道该按哪个去找。定位靠「v2.1 + 本附录的时间轴」，版本号跟着实现走（v2.33）已经够用。

**存放状态**：已入库 `docs/ERROR_SYSTEM_DESIGN.md`（2026-11-24），与 `docs/ERROR_CODES.md` 同目录。代码侧对应 v2.28–v2.33，HEAD `c338c344`。

入库时机是刻意的：先让实现跑通、再上线验收（`验证报告_DeepRead线上_v2.32_20261124.md`）、最后才把设计稿放进仓库。设计稿里回填了大量「实际做法与计划的出入」，这些出入只有实现完才知道；先入仓会变成一份看起来很准、其实是猜的计划书。
