#!/usr/bin/env python3
"""错误码表 —— 唯一事实来源。

设计见《设计_DeepRead错误体系_v2.1_20261124.md》第五节。三条约束：

1. **码稳定，文案自由**：码一旦发布不改含义，`message` / `hint` 可以随时改。
   测试断言码，不断言文案（见 tests/test_errors.py 与守门四）。
2. **分类与文案分离**：`condition` 是内部统计口径，同一 condition 可以有多个码
   （如 DR-1001 与 DR-2001 共用 incompleteRequest），不要按 condition 合并。
3. **两态**：`scope` 说明这个码可能出现在流前（pre）还是流中（in）。
   流前是 HTTP 4xx/5xx + JSON；流中只能写 SSE 帧（status: fail），HTTP 恒为 200。

本模块不依赖任何其它模块，也不发任何东西 —— 异常类从它取码，app 从它取文案。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

StageScope = Literal["pre", "in", "both"]
Responsibility = Literal["user", "third", "us"]


@dataclass(frozen=True)
class Code:
    key: str
    condition: str
    scope: StageScope
    http: int
    message: str
    hint: str
    retryable: bool
    responsibility: Responsibility
    log: bool


def _c(key, condition, scope, http, message, hint, retryable, resp, log) -> Code:
    return Code(key, condition, scope, http, message, hint, retryable, resp, log)


CODES: Final[dict[str, Code]] = {
    # ---------- 1xxx 输入 ----------
    "DR-1001": _c("DR-1001", "incompleteRequest", "pre", 400,
                  "请求内容为空", "重新提交", False, "user", False),
    "DR-1002": _c("DR-1002", "bodyTooLarge", "pre", 400,
                  "内容太大（上限 {limit}）", "{advice}", False, "user", False),
    "DR-1003": _c("DR-1003", "malformedJson", "pre", 400,
                  "请求格式有误", "刷新页面后重试", True, "user", False),
    # 1004 两态都用：流前 app 校验会发它，pipeline 的 extract 阶段也会发
    "DR-1004": _c("DR-1004", "noInput", "both", 400,
                  "没有可读解的内容", "提供链接、粘贴正文，或上传音视频", False, "user", False),
    "DR-1005": _c("DR-1005", "textTooShort", "pre", 400,
                  "正文太短（不足 200 字）", "粘贴完整内容，或改用链接 / 上传音视频",
                  False, "user", False),
    "DR-1006": _c("DR-1006", "textTooLong", "pre", 400,
                  "正文过长（上限 20 万字符）", "分两段读解", False, "user", False),
    "DR-1007": _c("DR-1007", "notFound", "pre", 404,
                  "接口不存在", "检查地址或刷新页面", False, "user", False),
    # ---------- 2xxx 上传 ----------
    "DR-2001": _c("DR-2001", "incompleteRequest", "pre", 400,
                  "上传中断", "重新上传", True, "user", False),
    "DR-2002": _c("DR-2002", "fileTooLarge", "pre", 413,
                  "文件超过上限 {limit} MB", "压缩后再传，或粘贴正文", False, "user", False),
    "DR-2003": _c("DR-2003", "unsupportedType", "pre", 400,
                  "不支持的文件类型 {ext}", "可选：{options}", False, "user", False),
    "DR-2004": _c("DR-2004", "invalidUploadId", "pre", 400,
                  "上传标识无效", "刷新页面重传", False, "user", False),
    "DR-2005": _c("DR-2005", "uploadExpired", "pre", 404,
                  "文件已过期（上传超 24 小时）", "重新上传", False, "user", False),
    "DR-2006": _c("DR-2006", "uploadIncomplete", "pre", 400,
                  "上传不完整", "重试一次", True, "user", False),
    # ---------- 3xxx 额度与并发 ----------
    "DR-3001": _c("DR-3001", "quotaExceeded", "pre", 429,
                  "本小时额度已用完（每小时 {limit} 次）", "约 {minutes} 分钟后恢复",
                  True, "user", False),
    "DR-3002": _c("DR-3002", "concurrencyFull", "pre", 503,
                  "同时读解的人较多", "等一两分钟再试", True, "user", False),
    "DR-3003": _c("DR-3003", "limiterUnavailable", "pre", 503,
                  "服务暂时繁忙", "等一两分钟再试", True, "third", True),
    # ---------- 4xxx 外部依赖 ----------
    "DR-4001": _c("DR-4001", "modelNotConfigured", "both", 200,
                  "服务端尚未配置模型", "把这个诊断信息发给我们", False, "us", True),
    "DR-4002": _c("DR-4002", "modelFailed", "in", 200,
                  "模型调用失败", "稍后重试（已自动重试 2 次）", True, "third", True),
    "DR-4003": _c("DR-4003", "fetchFailed", "in", 200,
                  "这个链接抓不到内容", "改用「粘贴正文」，或上传音视频", False, "third", True),
    "DR-4004": _c("DR-4004", "transcribeFailed", "in", 200,
                  "音视频转写失败", "换一个文件，或粘贴正文", True, "third", True),
    "DR-4005": _c("DR-4005", "mediaFailed", "in", 200,
                  "这个文件读不出来", "换格式重传", False, "third", True),
    "DR-4006": _c("DR-4006", "transcribeNotConfigured", "both", 200,
                  "服务端尚未配置转写", "把这个诊断信息发给我们", False, "us", True),
    # ---------- 5xxx 内部 ----------
    "DR-5001": _c("DR-5001", "frontendMissing", "pre", 500,
                  "页面文件缺失", "把这个诊断信息发给我们", False, "us", True),
    "DR-5002": _c("DR-5002", "storageWriteFailed", "pre", 500,
                  "服务器存储异常", "稍后重试；持续失败请发诊断信息", True, "us", True),
    "DR-5003": _c("DR-5003", "internalError", "both", 200,
                  "服务异常", "把这个诊断信息发给我们", False, "us", True),
}

# 降级标记（不是错误，不给码）。用户仍能拿到报告，只是内容打折。
NOTICES: Final[dict[str, str]] = {
    "search_unavailable": "检索暂不可用，本次未做溯源",
    "calc_untrusted": "实算未能可信完成，相关结论仅供参考",
}


def get(key: str) -> Code:
    """取一个码。未登记直接抛 —— 拼错码属于开发期错误，不该静默兜底。"""
    try:
        return CODES[key]
    except KeyError:
        raise KeyError(f"未登记的错误码：{key}（可用：{len(CODES)} 个）") from None


def present(c: Code) -> dict:
    """这条错误该怎么展示（设计 v2.1 第八节：由责任驱动，不由号段驱动）。

    **为什么放服务端算**：若前端自己按 responsibility + retryable 推一遍，
    25 个码的分档就活在前端一份隐式逻辑里——改一个码的展示口径要动两处，
    而且只能在浏览器里看出来。放这儿，Python 就能穷举 25 个码断言它。

    三档口径：
      user + 不可重试 → 红、不摆码（改一下就行，面前摆个 DR-1005 只会添乱）
      user + 可重试   → 黄、码放末尾小字
      third / us      → 黄 / 红，都摆码；third 是「换法 + 诊断」，us 是「诊断 + 复制」
    """
    is_user = c.responsibility == "user"
    return {
        "level": "fail" if (c.responsibility == "us" or (is_user and not c.retryable)) else "warn",
        "show_code": not (is_user and not c.retryable),
        "show_diag": not is_user,
    }


def payload(key: str, **fmt) -> dict:
    """构造返回体（不含 ok 与 request_id —— 这两样由调用方补，因为它知道请求上下文）。

    `responsibility` 与 `present()` 出的三个展示字段一起给前端：前端不该认识码，
    只该知道「这是谁的锅、红还是黄、要不要摆码和诊断」。把这层判断留在码表里，
    前端就不会抄出一份会漂的映射。

    文案一律走 `format(**fmt)`：模板里有占位符而调用方没传，就在开发期炸
    （KeyError），而不是把一句带 `{minutes}` 的原文发给用户。
    """
    c = get(key)
    out = {
        "code": c.key,
        "condition": c.condition,
        "error": c.message.format(**fmt),
        "hint": c.hint.format(**fmt),
        "retryable": c.retryable,
        "responsibility": c.responsibility,
    }
    out.update(present(c))
    return out


def keys_by_scope(scope: StageScope) -> list[str]:
    """守门用：列出某一态可能出现的码。"""
    return [k for k, c in CODES.items() if c.scope in (scope, "both")]


def all_keys() -> list[str]:
    return list(CODES)
