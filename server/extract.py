"""抓取层：把链接变成可核验的结构化文本。

三种来源分别处理，因为它们的坑完全不同：
  - 微信公众号：正文可能包在 JS 字符串里（需转义还原），块级标签必须映射为换行
    （踩过 </pre> 漏处理导致两段粘连、误判"原文缺句号"的坑），公式在 data-formula。
  - arXiv：abs 页给标题/作者序/日期/摘要；html 版给全文（不一定有）。
  - 通用网页：去脚本样式后按块级标签切段。

输出统一为 dict：url / kind / title / authors / published / text / n_chars / notes
notes 记录降级情况（如"仅取到摘要，无全文"），供下游在结论里如实标注。
"""

from __future__ import annotations

import html as htmllib
import os
import re
import time
from datetime import datetime, timedelta, timezone

import requests

UA_PC = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 元宝的微信内容分享页只对微信 UA 直出正文；PC UA 返回「请在微信客户端打开」
UA_WX = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.49(0x18003128) "
    "NetType/WIFI Language/zh_CN"
)

BLOCK_TAGS = [
    "p", "section", "div", "li", "ul", "ol", "pre", "blockquote",
    "h1", "h2", "h3", "h4", "h5", "h6", "tr", "table", "figure", "figcaption",
]


class FetchError(RuntimeError):
    """抓取失败：网络、反爬、结构变更。"""


def detect_kind(url: str) -> str:
    # 元宝的微信内容分享页（视频号走这条）：只对微信 UA 直出
    if "yb.tencent.com/wx/" in url or "yuanbao.tencent.com/wx/" in url:
        return "wechat_channel"
    if "channels.weixin.qq.com" in url:
        return "wechat_channel"
    if "mp.weixin.qq.com" in url:
        return "wechat"
    if re.search(r"arxiv\.org/(abs|pdf|html)/", url):
        return "arxiv"
    return "generic"


def _get(url: str, timeout: int = 25, retries: int = 2, ua: str | None = None) -> str:
    """带重试的 GET。真实世界里反爬与断流是常态，一次失败不代表不可达。"""
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            r = requests.get(
                url,
                headers={
                    "User-Agent": ua or UA_PC,
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                },
                timeout=timeout,
                allow_redirects=True,
            )
            if r.status_code >= 400:
                raise FetchError(f"HTTP {r.status_code}")
            ctype = (r.headers.get("Content-Type") or "").lower()
            # PDF / 二进制：抓下来也只是乱码，不如直接说清楚该怎么办
            if "application/pdf" in ctype:
                raise FetchError(
                    "这是 PDF，本服务不解析 PDF。请打开后复制文字，用「粘贴正文」入口"
                )
            if ctype and not any(k in ctype for k in ("html", "xml", "text/plain")):
                raise FetchError(f"不是网页内容（{ctype}），暂不支持")
            # 读取 body 也可能失败（chunked 断流），必须包在同一处重试里
            return r.content.decode(r.encoding or "utf-8", errors="replace")
        except FetchError:
            raise
        except requests.RequestException as exc:
            last = exc
            if attempt < retries:
                time.sleep(1.2 * (attempt + 1))
    raise FetchError(f"请求失败（已重试 {retries} 次）: {type(last).__name__}")


def _html_to_text(fragment: str, keep_formula: bool = True) -> str:
    """块级标签映射为换行，图片留一个标记，其余标签剥掉。"""
    s = fragment

    if keep_formula:
        # 微信公式在 data-formula 属性里（mdnice 渲染），直接还原 TeX 比解析 SVG 稳
        s = re.sub(
            r'<span[^>]*data-formula="([^"]*)"[^>]*>.*?</span>',
            lambda m: f" ${htmllib.unescape(m.group(1)).strip()}$ ",
            s,
            flags=re.S,
        )
        s = re.sub(
            r'<section[^>]*data-formula="([^"]*)"[^>]*>.*?</section>',
            lambda m: f"\n$${htmllib.unescape(m.group(1)).strip()}$$\n",
            s,
            flags=re.S,
        )

    # 图片常被当作段落分隔，必须留标记，否则两段会粘成一段（踩过的坑）
    s = re.sub(r"<img[^>]*>", "\n[图]\n", s, flags=re.I)
    for tag in BLOCK_TAGS:
        s = re.sub(r"</%s\s*>" % tag, "\n", s, flags=re.I)
        s = re.sub(r"<%s\b[^>]*>" % tag, "\n", s, flags=re.I)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<(script|style)\b.*?</\1>", "", s, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = htmllib.unescape(s)
    s = s.replace("\u3000", " ").replace("\xa0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n\n", s)
    return s.strip()


def _meta(html: str, *patterns: str) -> str:
    for pat in patterns:
        m = re.search(pat, html, re.S)
        if m:
            return htmllib.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()
    return ""


def _fetch_wechat(url: str) -> dict:
    html = _get(url)
    # 新版页面把正文包在 JS 字符串里
    html = (
        html.replace("\\x3c", "<")
        .replace("\\x3e", ">")
        .replace("\\x22", '"')
        .replace("\\x26", "&")
        .replace("\\x27", "'")
    )
    m = re.search(r'id="js_content"[^>]*>(.*?)<div id="js_tags"', html, re.S)
    if not m:
        m = re.search(
            r'id="js_content"[^>]*>(.*?)(?:</div>\s*<script|<div class="rich_media_tool)',
            html,
            re.S,
        )
    if not m:
        raise FetchError("未找到正文容器（js_content）：链接可能失效或页面结构变更")

    title = _meta(
        html,
        r'<h1[^>]*class="rich_media_title"[^>]*>(.*?)</h1>',
        r'<meta property="og:title" content="([^"]*)"',
    )
    account = _meta(html, r'id="js_name"[^>]*>(.*?)<', r'var nickname\s*=\s*"([^"]*)"')

    published = ""
    ts = re.search(r'var\s+ct\s*=\s*"(\d{10})"', html) or re.search(
        r'"(\d{10})"\s*\|\|\s*""', html
    )
    if ts:
        # var ct 是 UTC 秒，直接 fromtimestamp 会差 8 小时
        published = (
            datetime.fromtimestamp(int(ts.group(1)), tz=timezone.utc) + timedelta(hours=8)
        ).strftime("%Y-%m-%d %H:%M")

    text = _html_to_text(m.group(1))
    return {
        "url": url,
        "kind": "wechat",
        "title": title,
        "authors": [account] if account else [],
        "published": published,
        "text": text,
        "notes": [],
    }


def _unescape_layers(html: str) -> str:
    """元宝页面把 JSON 嵌在 JS 字符串里，套了 2–3 层反斜杠转义。"""
    s = html
    for _ in range(3):
        s = s.replace('\\\\"', '\\"').replace('\\"', '"')
    return s.replace("\\\\", "\\")


def _fetch_wechat_channel(url: str) -> dict:
    """视频号：经元宝分享页取「视频文案 + 元宝总结 + 元数据」。

    两个必须交代的事实（否则下游会误判）：
      1. 必须微信 UA，PC UA 只返回「请在微信客户端打开」；
      2. 拿到的**不是视频原话**，是元宝生成的总结。这个层级差写在 doc["layer"] 里，
         报告据此标注——不标，读者会以为在核原文。
    """
    html = _get(url, ua=UA_WX)
    if "微信视频号" not in html and "请在微信客户端打开" in html:
        raise FetchError("元宝分享页未直出内容（可能已失效或需微信内打开）。请用「粘贴正文」入口")

    s = _unescape_layers(html)
    i = s.find('"source":"微信视频号"')
    seg = s[max(0, i - 6000): i + 300] if i > 0 else s

    def field(key: str) -> str:
        m = re.search('"%s"\\s*:\\s*"([^"]*)"' % key, seg)
        return m.group(1) if m else ""

    titles = re.findall(r'"title"\s*:\s*"([^"]{2,120})"', s)
    title = next((t for t in titles if t not in ("元宝", "腾讯元宝")), "")
    account = field("author")
    desc = field("content")

    m_dur = re.search(r'"duration"\s*:\s*(\d+)', seg)
    duration = int(m_dur.group(1)) if m_dur else 0
    m_time = re.search(r'"answerTime"\s*:\s*"([^"]+)"', s)
    answer_time = m_time.group(1) if m_time else ""
    m_play = re.search(r'"(?:playUrl|url)"\s*:\s*"(https?://[^"]+)"', seg)
    play_url = m_play.group(1) if m_play else ""

    # 总结正文：页面可见文本，跳过导航与提示语
    t = _html_to_text(html, keep_formula=False)
    lines = [x.strip() for x in t.split("\n") if len(x.strip()) > 12]
    skip_kw = ("点击全选", "微信客户端", "默认浏览器", "来元宝", "听全文", "腾讯元宝")
    body = [x for x in lines if not any(k in x for k in skip_kw)]
    summary = "\n".join(body)
    summary_chars = sum(len(x) for x in body)
    if not summary:
        raise FetchError("未能从元宝页面提取到总结正文（页面结构可能已变），请用粘贴入口")

    parts = []
    if account:
        parts.append(f"视频号主体：{account}")
    if duration:
        parts.append(f"视频时长：{duration} 秒（约 {duration // 60} 分 {duration % 60} 秒）")
    if answer_time:
        parts.append(f"元宝整理日期：{answer_time}（不是视频发布时间）")
    if desc:
        parts.append(f"视频文案（发布者自述）：{desc}")
    parts.append(f"元宝生成的视频总结：\n{summary}")
    text = "\n\n".join(parts)

    notes = [
        "视频号：正文来自元宝对该视频的总结，不是视频原话（L1 二手）",
        "未取得视频发布时间与播放量；视频原声未做独立转录",
    ]
    if duration:
        spoken_lo, spoken_hi = duration * 4.0, duration * 5.0
        cov_lo = summary_chars / spoken_hi * 100
        cov_hi = summary_chars / spoken_lo * 100
        notes.append(
            f"总结 {summary_chars} 字；按时长估口播约 {spoken_lo:.0f}–{spoken_hi:.0f} 字，"
            f"覆盖率约 {cov_lo:.0f}%–{cov_hi:.0f}%"
        )

    return {
        "url": url,
        "kind": "wechat_channel",
        "layer": "L1 二手：元宝对视频的总结（非视频原话）",
        "title": title,
        "authors": [account] if account else [],
        "published": answer_time,
        "text": text,
        "summary_text": summary,   # 单独留一份：接了 ASR 之后要拿它跟原声对照
        "duration_sec": duration,
        "summary_chars": summary_chars,
        "desc": desc,
        "play_url": play_url,
        "notes": notes,
    }


def _fetch_arxiv(url: str) -> dict:
    abs_url = re.sub(r"arxiv\.org/(pdf|html)/", "arxiv.org/abs/", url)
    html = _get(abs_url)
    title = _meta(html, r'<h1 class="title[^"]*">(.*?)</h1>')
    title = re.sub(r"^Title:\s*", "", title)

    # 作者序是硬事实，必须逐个取 <a>，不能按逗号 split（姓名里可能有逗号）
    authors_html = _meta(html, r'<div class="authors">(.*?)</div>')
    authors_html = re.sub(r"^Authors:\s*", "", authors_html)
    authors = [
        htmllib.unescape(re.sub(r"<[^>]+>", "", a)).strip()
        for a in re.findall(r"<a[^>]*>(.*?)</a>", authors_html, re.S)
    ]
    if not authors and authors_html:
        authors = [a.strip() for a in authors_html.split(",") if a.strip()]

    published = _meta(html, r'<div class="dateline">(.*?)</div>').strip("[] ")
    abstract = _meta(html, r'<blockquote class="abstract[^"]*">(.*?)</blockquote>')
    abstract = re.sub(r"^Abstract:\s*", "", abstract)

    text = abstract
    notes = ["arxiv: 仅取到摘要，全文未取"]
    m = re.search(r"arxiv\.org/abs/([\w.\-/]+)", abs_url)
    if m:
        html_url = f"https://arxiv.org/html/{m.group(1)}"
        try:
            raw = _get(html_url)
            # 只切正文容器，否则导航/目录/主题切换条会全部混进来
            art = re.search(r"<article\b[^>]*>(.*?)</article>", raw, re.S | re.I)
            body = art.group(1) if art else raw
            body = re.sub(
                r'<(nav|header|footer)\b.*?</\1>', "", body, flags=re.S | re.I
            )
            full = _html_to_text(body)
            if len(full) > len(abstract) * 2:
                text = full
                notes = ["arxiv: 已取到 HTML 全文"]
        except FetchError:
            pass
    return {
        "url": abs_url,
        "kind": "arxiv",
        "title": title,
        "authors": authors,
        "published": published,
        "text": text,
        "notes": notes,
    }


def _fetch_generic(url: str) -> dict:
    html = _get(url)
    title = _meta(html, r'<meta property="og:title" content="([^"]*)"', r"<title>(.*?)</title>")
    m = re.search(r"<article\b[^>]*>(.*?)</article>", html, re.S | re.I)
    body = m.group(1) if m else re.sub(
        r"<(script|style|nav|header|footer|aside)\b.*?</\1>", "", html, flags=re.S | re.I
    )
    text = _html_to_text(body)
    notes = ["generic: 按标签剥离抽取，结构复杂页面可能有噪声"]
    return {
        "url": url,
        "kind": "generic",
        "title": title,
        "authors": [],
        "published": "",
        "text": text,
        "notes": notes,
    }


def deepseek_fetch(url: str, timeout: int = 60) -> dict | None:
    """用 DeepSeek /v1/tools/fetch 抓网页，返回 {title, content(markdown)}。

    它解决的是反爬：公众号、需渲染的页面，本地正则常常拿不到正文。
    拿不到就返回 None，由调用方回退，不抛异常。
    """
    key = os.environ.get("DFS_API_KEY") or os.environ.get("LLM_API_KEY", "")
    if not key:
        return None
    root = os.environ.get("DFS_BASE_URL", "https://api.deepseek.com").rstrip("/")
    if not root.endswith("/v1"):
        root += "/v1"
    try:
        r = requests.post(
            root + "/tools/fetch",
            json={"url": url},
            headers={"Authorization": f"Bearer {key}"},
            timeout=timeout,
        )
        if r.status_code >= 400:
            return None
        d = r.json()
        return {"title": str(d.get("title", "")), "content": str(d.get("content", ""))}
    except (requests.RequestException, ValueError):
        return None


def _finish(doc: dict) -> dict:
    doc["n_chars"] = len(doc.get("text", ""))
    if doc["n_chars"] < 200:
        doc.setdefault("notes", []).append(
            f"正文仅 {doc['n_chars']} 字：可能是 JS 动态渲染的页面或反爬拦截。"
            "请改用「粘贴正文」入口，或配置 DFS_API_KEY 让 DeepSeek Fetch 兜底"
        )
    return doc


def _via_deepseek(url: str, reason: str) -> dict | None:
    got = deepseek_fetch(url)
    if not got or len(got.get("content", "")) < 400:
        return None
    return {
        "url": url,
        "kind": "dfs_fetch",
        "title": got.get("title", ""),
        "authors": [],
        "published": "",
        "text": got["content"],
        "notes": [
            f"由 DeepSeek /v1/tools/fetch 抓取（{reason}）；"
            "作者、发布时间未从页面解析，相关核对只能标「未核」"
        ],
    }


def fetch(url: str) -> dict:
    """抓取并按来源分派。

    FETCH_BACKEND:
      auto（默认）  本地解析优先，失败才回退 DeepSeek Fetch —— 保住作者/时间解析
      deepseek      DeepSeek Fetch 优先，失败回退本地 —— 面对强反爬时更省事
    """
    url = url.strip()
    if not re.match(r"^https?://", url):
        raise FetchError("请提供 http(s) 开头的完整链接")

    kind = detect_kind(url)
    local = {
        "wechat": _fetch_wechat,
        "wechat_channel": _fetch_wechat_channel,
        "arxiv": _fetch_arxiv,
        "generic": _fetch_generic,
    }[kind]
    backend = (os.environ.get("FETCH_BACKEND") or "auto").strip().lower()

    if backend == "deepseek":
        doc = _via_deepseek(url, "按 FETCH_BACKEND=deepseek 优先")
        if doc:
            return _finish(doc)
        try:
            doc = local(url)
            doc["notes"].append("DeepSeek Fetch 未取到有效正文，已回退本地抓取")
            return _finish(doc)
        except FetchError as exc:
            raise FetchError(f"DeepSeek Fetch 与本地抓取都失败：{exc}") from exc

    try:
        return _finish(local(url))
    except FetchError as exc:
        doc = _via_deepseek(url, f"本地抓取失败：{exc}")
        if doc:
            return _finish(doc)
        raise


if __name__ == "__main__":
    import json
    import sys

    d = fetch(sys.argv[1])
    d["text"] = d["text"][:600] + ("…" if len(d["text"]) > 600 else "")
    print(json.dumps(d, ensure_ascii=False, indent=2))
