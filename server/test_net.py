#!/usr/bin/env python3
"""TLS 出口的回归用例。

对应 v1.4 实况：Windows 上报
    SSLError(CERTIFICATE_VERIFY_FAILED ... unable to get local issuer certificate)
代码里没有任何 CA 配置、没有重试、也没把错误翻成人话。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import requests  # noqa: E402

import net  # noqa: E402


def main() -> int:
    bad = 0

    def chk(name, cond):
        nonlocal bad
        print(("[ok] " if cond else "[X ] ") + name)
        if not cond:
            bad += 1

    saved = {k: os.environ.get(k) for k in
             ("CA_BUNDLE", "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE", "INSECURE_TLS")}

    def clean():
        for k in saved:
            os.environ.pop(k, None)

    try:
        # ---- 默认：走 certifi（verify=True）----
        clean()
        chk("默认 verify=True", net.ca_bundle() is True)
        chk("默认不是 insecure", net.insecure() is False)

        # ---- 显式 CA 文件存在则采用 ----
        tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp_ca.pem")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("dummy")
        os.environ["CA_BUNDLE"] = tmp
        chk("CA_BUNDLE 生效", net.ca_bundle() == tmp)

        # ---- 路径不存在则不采信（避免把打错的路径当成证书）----
        os.environ["CA_BUNDLE"] = "/no/such/file.pem"
        chk("路径不存在时忽略", net.ca_bundle() is True)
        clean()

        # ---- 开关语义 ----
        for v in ("1", "true", "YES", "on"):
            os.environ["INSECURE_TLS"] = v
            chk(f"INSECURE_TLS={v} 视为开启", net.insecure() is True)
        os.environ["INSECURE_TLS"] = "0"
        chk("INSECURE_TLS=0 视为关闭", net.insecure() is False)
        clean()
        os.unlink(tmp)

        # ---- 错误翻译 ----
        ssl_exc = requests.exceptions.SSLError(
            "HTTPSConnectionPool: SSLError(SSLCertVerificationError(1, "
            "'[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed'))")
        txt = net.explain(ssl_exc)
        chk("SSL 错误提到 certifi", "certifi" in txt)
        chk("SSL 错误给出 CA_BUNDLE 出路", "CA_BUNDLE" in txt)
        chk("SSL 错误提到中间人/代理", "中间人" in txt or "代理" in txt)

        to_exc = requests.exceptions.Timeout("Read timed out")
        chk("超时有专门文案", "超时" in net.explain(to_exc))

        conn_exc = requests.exceptions.ConnectionError("Failed to establish")
        chk("连接失败有专门文案", "连不上" in net.explain(conn_exc))

        # ---- 重试：SSLError 应当重试，4xx 不重试 ----
        calls = {"n": 0}
        real_post = requests.post

        def flaky(url, **kw):
            calls["n"] += 1
            raise requests.exceptions.SSLError("boom")

        requests.post = flaky
        try:
            try:
                net.post("https://x/y", retries=2)
                chk("SSLError 最终仍抛出", False)
            except requests.exceptions.SSLError:
                chk("SSLError 最终仍抛出", True)
            chk("SSLError 重试 3 次（1+2）", calls["n"] == 3)
        finally:
            requests.post = real_post

        calls["n"] = 0

        def once_ok(url, **kw):
            calls["n"] += 1
            return "resp"

        requests.post = once_ok
        try:
            r = net.post("https://x/y", retries=2)
            chk("成功不重试", r == "resp" and calls["n"] == 1)
        finally:
            requests.post = real_post

        # ---- 重试第二次成功 ----
        calls["n"] = 0

        def fail_once(url, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise requests.exceptions.Timeout("t")
            return "resp2"

        requests.post = fail_once
        try:
            r = net.post("https://x/y", retries=2)
            chk("失败一次后重试成功", r == "resp2" and calls["n"] == 2)
        finally:
            requests.post = real_post

    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    print("\n" + ("TLS 出口通过" if bad == 0 else f"{bad} 项失败"))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
