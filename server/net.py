"""统一的 HTTP 出口：证书、超时、重试、错误翻译。

Windows 上最常见的失败不是网络不通，而是证书链找不到：

    SSLError(SSLCertVerificationError(1, 'CERTIFICATE_VERIFY_FAILED
    unable to get local issuer certificate'))

三个成因，按出现频率排：
  1. certifi 缺失或过期（Python 自带的信任库不是系统的）
  2. 安全软件/代理在做 HTTPS 中间人（火绒、360、卡巴、Clash、Fiddler…），
     它签的证书不在信任库里
  3. 企业根证书没装进系统的信任库

这里**不默认关闭校验** —— 那只会把问题掩盖成「能跑但不知道在跟谁说话」。
默认走 certifi，可以用环境变量指定 CA，也可以在明确知情时关掉。
"""
from __future__ import annotations

import os
import time

import requests

_INSECURE_VALUES = ("1", "true", "yes", "on")


def insecure() -> bool:
    return os.environ.get("INSECURE_TLS", "").strip().lower() in _INSECURE_VALUES


def ca_bundle():
    """返回 requests 的 verify 参数。

    优先级：显式 CA 文件 > 关闭校验（用户明确开了）> 默认信任库。
    """
    for key in ("CA_BUNDLE", "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE"):
        path = os.environ.get(key, "").strip()
        if path and os.path.exists(path):
            return path
    if insecure():
        return False
    return True


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, requests.exceptions.SSLError):
        return True                      # 偶发的握手失败，重试常能过去
    if isinstance(exc, (requests.exceptions.ConnectionError,
                        requests.exceptions.Timeout)):
        return True
    return False


def post(url: str, *, retries: int = 2, **kw):
    """POST 一版带重试与统一 CA 的封装。

    body 必须是可重复发送的（json=/data=/bytes），不要传已打开的 file 对象——
    重试时那个句柄已经读到末尾了。
    """
    kw.setdefault("verify", ca_bundle())
    kw.setdefault("timeout", 120)
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return requests.post(url, **kw)
        except requests.RequestException as exc:
            last = exc
            if attempt < retries and _retryable(exc):
                time.sleep(min(2 ** attempt, 4))
                continue
            raise
    raise last  # pragma: no cover


def explain(exc: Exception) -> str:
    """把底层异常翻成「下一步该做什么」，而不是把 traceback 丢给用户。"""
    msg = str(exc)
    if isinstance(exc, requests.exceptions.SSLError) or "CERTIFICATE_VERIFY_FAILED" in msg:
        return (
            "TLS 证书校验失败（不是网络不通，也不是 API key 错）。按顺序试：\n"
            "  1) 升级根证书：python3 -m pip install --upgrade certifi\n"
            "     Windows 仍失败时再装：python3 -m pip install python-certifi-win32\n"
            "  2) 指定证书：set CA_BUNDLE=D:\\path\\to\\ca.pem（公司代理的根证书）\n"
            "  3) 检查有没有安全软件/代理在做 HTTPS 中间人"
            "（火绒、360、卡巴、Clash、Fiddler 等），关掉或把它的根证书装进系统\n"
            "  仅在明确知情、只顾本机调试时，可 set INSECURE_TLS=1 关闭校验"
        )
    if isinstance(exc, requests.exceptions.Timeout) or "timed out" in msg.lower():
        return "请求超时：网络慢或服务端排队。稍后重试；文件大时可调大 ASR_TIMEOUT。"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return ("连不上服务端：检查网络、DNS、代理设置。"
                "若有 HTTPS_PROXY 环境变量指向失效代理，请清掉它。")
    return str(exc)


def describe() -> str:
    """一句话说明当前的 TLS 配置，供 doctor 输出与日志使用。"""
    if insecure():
        return "TLS 校验已关闭（INSECURE_TLS=1）"
    v = ca_bundle()
    if isinstance(v, str):
        return f"TLS 使用指定证书：{v}"
    try:
        import certifi
        return f"TLS 使用 certifi：{certifi.where()}"
    except ImportError:
        return "TLS 使用系统信任库（certifi 未安装）"
