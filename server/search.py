"""DeepSeek Search 适配层：给「未核」的条目找外部出处。

接口（2026-11 官方文档）：
    POST {root}/tools/search
    {"query": "...", "count": 10, "need_content": false, "ori_question": "..."}
    → {"results": [{rerank_score, title, url, snippet, summary, site_name,
                    date, authority}]}

两个分数决定了它能不能当证据：
    rerank_score  0–3，≥1.5 视为高质量
    authority     0–4，≥3 视为高权威（-1 未知）

设计原则：**只给「未核」的条目检索**。已经能判的不要重复花钱；
检索结果不足以下结论时，宁可保持「未核」，不许用低质量结果硬升成「确证」。
"""

from __future__ import annotations

import os
import re
from typing import Any

import net
import requests

MAX_QUERY = 128          # API 限制
GOOD_RERANK = 1.5
GOOD_AUTHORITY = 3


class SearchError(RuntimeError):
    """检索失败 —— 这是降级不是错误：流程继续，报告标「未溯源」。"""
    notice = "search_unavailable"


class SearchNotConfigured(SearchError):
    """没配 key —— 上层据此跳过溯源，报告里如实写「未溯源」。"""
    notice = "search_unavailable"


def config() -> dict:
    return {
        "enabled": os.environ.get("SEARCH_ENABLED", "auto").strip().lower(),
        "base_url": os.environ.get("DFS_BASE_URL", "https://api.deepseek.com").rstrip("/"),
        "api_key": (os.environ.get("DFS_API_KEY")
                    or os.environ.get("LLM_API_KEY", "")),
        "count": int(os.environ.get("SEARCH_COUNT", "6")),
        "max_items": int(os.environ.get("SEARCH_MAX_ITEMS", "3")),
    }


def is_configured() -> bool:
    cfg = config()
    if cfg["enabled"] in ("off", "0", "false", "no"):
        return False
    return bool(cfg["api_key"])


def describe() -> str:
    if not is_configured():
        return "未启用"
    cfg = config()
    return f"deepseek /tools/search · 每次取 {cfg['count']} 条 · 最多溯源 {cfg['max_items']} 项"


def _root() -> str:
    b = config()["base_url"].rstrip("/")
    return b if b.endswith("/v1") else b + "/v1"


def search(query: str, count: int | None = None, need_content: bool = False,
           timeout: int = 30) -> list[dict]:
    """执行一次检索，返回 results 列表（已按质量排序）。失败抛 SearchError。"""
    cfg = config()
    if not cfg["api_key"]:
        raise SearchNotConfigured("SEARCH/DFS/LLM 均未配置 key")
    q = (query or "").strip()
    if not q:
        raise SearchError("查询为空")
    if len(q) > MAX_QUERY:
        q = q[:MAX_QUERY]
    payload: dict[str, Any] = {
        "query": q,
        "count": int(count or cfg["count"]),
        "need_content": bool(need_content),
    }
    try:
        r = net.post(
            _root() + "/tools/search",
            json=payload,
            headers={"Authorization": f"Bearer {cfg['api_key']}"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise SearchError(f"检索请求失败：{net.explain(exc)}") from exc
    if r.status_code >= 400:
        raise SearchError(f"Search HTTP {r.status_code}: {r.text[:200]}")
    try:
        data = r.json()
    except ValueError as exc:
        raise SearchError(f"响应不是 JSON: {r.text[:200]}") from exc
    results = data.get("results") or []
    return [_normalize(x) for x in results if isinstance(x, dict)]


def _normalize(x: dict) -> dict:
    def num(key, default=0.0):
        try:
            return float(x.get(key))
        except (TypeError, ValueError):
            return default

    return {
        "title": str(x.get("title", "")).strip(),
        "url": str(x.get("url", "")).strip(),
        "snippet": str(x.get("snippet", "")).strip(),
        "summary": str(x.get("summary", "")).strip(),
        "site": str(x.get("site_name") or "").strip(),
        "date": str(x.get("date") or "").strip(),
        "rerank": num("rerank_score", 0.0),
        "authority": int(num("authority", -1)),
    }


# 二手平台：内容可能是原发也可能是转载，但无法确认独立性，永远不能支撑「确证」。
# 判据必须是域名，不能是搜索接口的 authority 分 —— 那一分衡量的是「相关性/质量」，
# 不衡量「来源是否一手」。实测：mp.weixin.qq.com 拿到 authority=3，于是公众号
# 被当成权威源去支撑「确证」，等于给自媒体背书。
_SECONDHAND = (
    "mp.weixin.qq.com", "weixin.qq.com", "toutiao.com", "sina.com.cn", "sina.cn",
    "sohu.com", "baijiahao.baidu.com", "163.com", "csdn.net", "zhihu.com",
    "jianshu.com", "baike.baidu.com", "wikipedia.org", "mydrivers.com",
    "cnki.net", "21jingji.com", "eet-china.com", "ithome.com", "cnbeta.com",
    "ifeng.com", "thepaper.cn", "guancha.cn", "36kr.com", "huxiu.com",
    "weibo.com", "bilibili.com", "douban.com", "xuewen.cnki.net", "sohu",
)

# 一手来源：机构官网、学术出版、标准组织、监管文件 —— 可以单独支撑「确证」
_PRIMARY = (
    # 学术出版与预印本
    "acm.org", "nature.com", "science.org", "ieee.org", "springer.com",
    "sciencedirect.com", "arxiv.org", "semanticscholar.org", "pubmed.ncbi.nlm.nih.gov",
    "zenodo.org", "ssrn.com", "nber.org", "jstor.org", "tandfonline.com",
    "wiley.com", "sagepub.com", "cell.com", "plos.org", "frontiersin.org",
    "mdpi.com", "iopscience.iop.org", "aps.org", "acs.org", "rsc.org",
    # 标准组织、监管与行业一手数据
    "semi.org", "semiconductors.org", "iso.org", "iec.ch", "itu.int",
    "sec.gov", "europa.eu", "oecd.org", "worldbank.org", "imf.org",
    "who.int", "un.org", "stats.gov.cn",
    # 相关厂商官网（读解题目常涉及的公司，官方口径优先于媒体报道）
    "asml.com", "zeiss.com", "tsmc.com", "nvidia.com", "intel.com",
    "samsung.com", "micron.com", "appliedmaterials.com", "lamresearch.com",
    "kla.com", "synopsys.com", "cadence.com", "siemens.com",
    "deepseek.com", "openai.com", "anthropic.com", "google.com",
)


def _host(url: str) -> str:
    m = re.match(r"^https?://([^/]+)", url or "", re.I)
    return (m.group(1) if m else "").lower().split(":")[0]


host_of = _host   # 公开别名，供 pipeline 做域名比较


def _match(host: str, domains: tuple) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def is_authoritative_host(host: str) -> bool:
    """某个域名是否属于一手来源。用于核对模型在依据里自标的「（权威源）」。"""
    h = (host or "").lower().strip()
    if not h:
        return False
    if _match(h, _SECONDHAND):
        return False
    return (_match(h, _PRIMARY) or h.endswith(".gov") or ".gov." in h
            or h.endswith(".edu") or ".edu." in h
            or h.endswith(".int") or ".int." in h)


def quality(hit: dict) -> tuple[str, str]:
    """证据分三级。

    关键区分：**相关 ≠ 权威**。一个公众号讲得很贴题，rerank 能到 2.5，
    但它只是二手转述，不能用来判「确证」。实测踩过：把「微信公众平台」
    「新浪财经转载」当成确证依据，等于给自媒体背书。

    判定顺序是「域名优先」而不是「分数优先」：
      1. 命中二手平台 → 直接 relevant，分数再高也不动摇；
      2. 命中一手来源 → authoritative；
      3. 都不命中 → 才看 authority 分。

        authoritative  权威源，可单独支撑「确证」
        relevant       相关但非权威，只能作旁证或支撑「半对」
        weak           质量不足，仅供参考
    """
    host = _host(hit.get("url", ""))
    if _match(host, _SECONDHAND):
        return "relevant", f"二手平台（{host}），不能支撑确证"
    if is_authoritative_host(host):
        return "authoritative", f"一手来源（{host}）"
    # 名单外一律不认权威 —— 这是白名单制，不是打分制。
    # 搜索接口的 authority 分把「相关性」也算进去了，实测三立新闻网、日经中文网、
    # 艾瑞咨询都能拿到高分；它们是好媒体，但仍是转述，不能支撑「确证」。
    if hit["authority"] >= GOOD_AUTHORITY or hit["rerank"] >= GOOD_RERANK:
        return "relevant", (f"非一手来源（{host or '未知域名'}，"
                            f"authority {hit['authority']} / rerank {hit['rerank']:.1f}）")
    return "weak", f"质量不足（rerank {hit['rerank']:.1f} / authority {hit['authority']}）"


def evidence_for(query: str, limit: int = 3) -> dict:
    """按查询词取证据，分三桶返回。"""
    hits = search(query)
    ranked = sorted(hits, key=lambda h: (h["authority"], h["rerank"]), reverse=True)
    buckets = {"authoritative": [], "relevant": [], "weak": []}
    for h in ranked:
        tag, why = quality(h)
        h["why"] = why
        buckets[tag].append(h)
    return {
        "query": query,
        "authoritative": buckets["authoritative"][:limit],
        "relevant": buckets["relevant"][:limit],
        "weak": buckets["weak"][:2],
        "total": len(hits),
    }


def domain_of(url: str) -> str:
    """从 URL 取域名，报告里显示出处用。"""
    u = (url or "").strip()
    for prefix in ("https://", "http://"):
        if u.startswith(prefix):
            u = u[len(prefix):]
    host = u.split("/")[0]
    return host[4:] if host.startswith("www.") else host
