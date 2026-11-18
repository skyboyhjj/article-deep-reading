"""LLM 接口（OpenAI 兼容）。

只依赖环境变量，不绑厂商：
    LLM_BASE_URL  默认 https://api.deepseek.com/v1
    LLM_API_KEY   必填
    LLM_MODEL     默认 deepseek-chat

这样换模型只改环境变量：通义（dashscope.aliyuncs.com/compatible-mode/v1）、
Moonshot、本地 vLLM 都按同一套协议接。
"""

from __future__ import annotations

import json
import os
import re

import net
import requests


class LLMError(RuntimeError):
    pass


class LLMNotConfigured(LLMError):
    """未配置 key —— 服务应据此返回 503，而不是静默降级。"""


def config() -> dict:
    return {
        "base_url": os.environ.get("LLM_BASE_URL", "https://api.deepseek.com/v1").rstrip("/"),
        "api_key": os.environ.get("LLM_API_KEY", ""),
        "model": os.environ.get("LLM_MODEL", "deepseek-chat"),
    }


def is_configured() -> bool:
    return bool(config()["api_key"])


def chat(
    messages: list[dict],
    *,
    temperature: float = 0.2,
    max_tokens: int = 2048,
    json_mode: bool = False,
    timeout: int = 180,
) -> str:
    cfg = config()
    if not cfg["api_key"]:
        raise LLMNotConfigured("LLM_API_KEY 未设置")

    payload = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    try:
        r = net.post(
            f"{cfg['base_url']}/chat/completions",
            headers={
                "Authorization": f"Bearer {cfg['api_key']}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise LLMError(f"模型调用失败：{net.explain(exc)}") from exc

    if r.status_code >= 400:
        raise LLMError(f"LLM HTTP {r.status_code}: {r.text[:300]}")
    try:
        return r.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError) as exc:
        raise LLMError(f"响应格式异常: {r.text[:300]}") from exc


def _salvage_json(raw: str) -> dict | None:
    """从被截断的 JSON 里抢救出完整的那部分。

    模型输出超长被切断时，末尾半截对象是废的，但前面的元素都是好的。
    做法：走一遍找最后一个「逗号处」（那里截断后补括号仍是合法 JSON），
    把栈里未闭合的括号补上。
    """
    i = raw.find("{")
    if i < 0:
        return None
    s = raw[i:]
    stack: list[str] = []
    in_str = False
    esc = False
    last_safe = None
    for k, ch in enumerate(s):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack:
                stack.pop()
            if not stack:
                try:
                    return json.loads(s[: k + 1])
                except json.JSONDecodeError:
                    return None
        elif ch == "," and stack:
            last_safe = (k, list(stack))
    if not last_safe:
        return None
    k, stack = last_safe
    closing = "".join("}" if c == "{" else "]" for c in reversed(stack))
    try:
        return json.loads(s[:k] + closing)
    except json.JSONDecodeError:
        return None


def chat_json(messages: list[dict], **kw) -> dict:
    """取回 JSON。模型常把 JSON 包在 ```json 里，这里做容错。"""
    raw = chat(messages, json_mode=True, **kw)
    s = raw.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", s, re.S)
    if m:
        s = m.group(1).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        # 兜底：抓第一个完整的大括号块
        i, j = s.find("{"), s.rfind("}")
        if i >= 0 and j > i:
            try:
                return json.loads(s[i : j + 1])
            except json.JSONDecodeError:
                pass
        # 输出被 max_tokens 截断时，抢救出前面完整的部分（总比整个任务失败好）
        salvaged = _salvage_json(raw)
        if salvaged is not None:
            return salvaged
    raise LLMError(f"无法解析为 JSON: {raw[:300]}")
