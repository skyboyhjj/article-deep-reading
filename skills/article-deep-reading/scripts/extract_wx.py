import re
import sys

path = sys.argv[1] if len(sys.argv) > 1 else "wx_lg.html"
out = sys.argv[2] if len(sys.argv) > 2 else "wx_lg.txt"

h = open(path, encoding="utf-8", errors="replace").read()

# 新版页面正文可能包在 JS 字符串里
h = (h.replace("\\x3c", "<").replace("\\x3e", ">")
      .replace("\\x22", '"').replace("\\x26", "&").replace("\\x27", "'")
      .replace("\\n", "\n"))

m = re.search(r'id="js_content"[^>]*>(.*?)<div id="js_tags"', h, re.S)
if not m:
    m = re.search(r'id="js_content"[^>]*>(.*?)(?:</div>\s*<script|<div class="rich_media_tool)', h, re.S)
if not m:
    print("NOT FOUND js_content")
    sys.exit(1)
body = m.group(1)

# 块级闭合标签 -> 换行（含 pre / li / section / blockquote，踩过 pre 的坑）
for tag in ["p", "section", "div", "li", "ul", "ol", "pre", "blockquote",
            "h1", "h2", "h3", "h4", "tr", "table", "figure", "figcaption"]:
    body = re.sub(r"</%s\s*>" % tag, "\n", body, flags=re.I)
    body = re.sub(r"<%s[^>]*>" % tag, "\n", body, flags=re.I)
body = re.sub(r"<br\s*/?>", "\n", body, flags=re.I)
# 图片留标记（图片常被当作段落分隔）
body = re.sub(r"<img[^>]*>", "\n[[图]]\n", body, flags=re.I)
body = re.sub(r"<svg.*?</svg>", "\n[[SVG]]\n", body, flags=re.S | re.I)
# 公式
body = re.sub(r'data-formula="([^"]*)"', lambda m: " $`" + m.group(1) + "`$ ", body, flags=re.I)
# 强调
body = re.sub(r"</strong\s*>", "」", body, flags=re.I)
body = re.sub(r"<strong[^>]*>", "「", body, flags=re.I)
# 其余标签
body = re.sub(r"<[^>]+>", "", body)
body = (body.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
        .replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'")
        .replace("&ldquo;", "「").replace("&rdquo;", "」"))

lines = [re.sub(r"[ \t\u00a0]+", " ", ln).strip() for ln in body.split("\n")]
lines = [ln for ln in lines if ln]
text = "\n".join(lines)

open(out, "w", encoding="utf-8").write(text)
print("chars:", len(text), "lines:", len(lines), "imgs:", text.count("[[图]]"))
print("---- head ----")
print(text[:1500])
