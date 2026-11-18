#!/usr/bin/env python3
"""体检：证书链、代理、连通性。不打印任何 key。

用法：python3 server/doctor.py
用来定位 SSLError(CERTIFICATE_VERIFY_FAILED) 这类「代码没毛病、环境有毛病」的故障。
"""
from __future__ import annotations

import os
import platform
import socket
import ssl
import sys

HOSTS = ("api.deepseek.com",)


def line(title: str) -> None:
    print(f"\n=== {title} ===")


def check_python() -> None:
    line("Python")
    print(f"版本    {sys.version.split()[0]}  ({platform.system()} {platform.release()})")
    print(f"解释器  {sys.executable}")


def check_certifi() -> None:
    line("根证书（certifi）")
    try:
        import certifi
    except ImportError:
        print("未安装 —— 这就是 SSL 失败的常见原因")
        print("  修：python3 -m pip install certifi")
        print("  Windows 还可加：python3 -m pip install python-certifi-win32")
        return
    where = certifi.where()
    print(f"路径    {where}")
    print(f"存在    {os.path.exists(where)}")
    if not os.path.exists(where):
        print("  文件不存在 → pip install --upgrade --force-reinstall certifi")
        return
    try:
        with open(where, "rb") as fh:
            blob = fh.read()
        n = blob.count(b"-----BEGIN CERTIFICATE-----")
        print(f"证书数  {n}")
        if n < 100:
            print("  数量偏少（正常 100+），可能不是完整 bundle")
    except OSError as exc:
        print(f"读取失败：{exc}")


def _mask(url: str) -> str:
    """代理地址里可能带账号密码，打印时遮蔽。"""
    if "@" not in url:
        return url
    head, _, tail = url.rpartition("@")
    scheme = head.split("://")[0] + "://" if "://" in head else ""
    return scheme + "***@" + tail


def check_env() -> None:
    line("相关环境变量")
    keys = ("CA_BUNDLE", "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE", "SSL_CERT_DIR",
            "INSECURE_TLS", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
            "http_proxy", "https_proxy", "all_proxy", "NO_PROXY")
    found = False
    for k in keys:
        v = os.environ.get(k)
        if not v:
            continue
        found = True
        print(f"{k:18} {_mask(v)}")
    if not found:
        print("（无）")
    if os.environ.get("INSECURE_TLS", "").strip().lower() in ("1", "true", "yes", "on"):
        print("\n注意：INSECURE_TLS 已开启 —— 证书校验被关闭，仅限本机调试。")


def check_tls(host: str) -> None:
    line(f"TLS 握手 {host}")
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(certifi.where())
        print("信任库  certifi")
    except ImportError:
        print("信任库  系统默认")
    try:
        with socket.create_connection((host, 443), timeout=8) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ss:
                cert = ss.getpeercert()
                issuer = dict(x[0] for x in cert.get("issuer", ()))
                subject = dict(x[0] for x in cert.get("subject", ()))
                print(f"协议    {ss.version()}")
                print(f"颁发者  {issuer.get('organizationName', '?')}")
                print(f"主体    {subject.get('commonName', '?')}")
                print(f"有效期  {cert.get('notBefore')} → {cert.get('notAfter')}")
                print("结果    成功")
    except ssl.SSLCertVerificationError as exc:
        print(f"结果    证书校验失败：{exc.verify_message}")
        print("        → 中间人代理或根证书缺失，见文末建议")
    except ssl.SSLError as exc:
        print(f"结果    TLS 错误：{exc}")
    except (socket.timeout, OSError) as exc:
        print(f"结果    连不上：{exc}")


def check_requests() -> None:
    line("requests 实际使用的配置")
    try:
        import net
        print(f"当前      {net.describe()}")
        print(f"verify    {net.ca_bundle()!r}")
    except Exception as exc:  # noqa: BLE001
        print(f"读取 net.py 失败：{exc}")


def suggestion() -> None:
    line("如果上面有失败项")
    print("""1) 升级根证书
     python3 -m pip install --upgrade certifi
     Windows 仍失败再装：python3 -m pip install python-certifi-win32

2) 公司/代理的根证书
     set CA_BUNDLE=D:\\path\\to\\company-ca.pem

3) 关掉做 HTTPS 中间人的软件（火绒、360、卡巴、Clash、Fiddler…），
   或把它的根证书导入系统信任库后再跑

4) 本机调试、明确知情时的最后手段
     set INSECURE_TLS=1
   这会让所有 HTTPS 不再校验身份，等于放弃防护，别在真实环境里用。""")


def main() -> int:
    print("读解服务 · 环境体检")
    check_python()
    check_certifi()
    check_env()
    check_requests()
    for h in HOSTS:
        check_tls(h)
    suggestion()
    return 0


if __name__ == "__main__":
    sys.exit(main())
