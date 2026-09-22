"""SSRF 出网护栏(L1)。

worker 会去抓取第三方 URL(TTS / stock 素材)。这些 URL 来自上游供应商
应答,虽受信,但**一旦上游被攻陷或能被诱导返回恶意 URL**,`urllib.urlopen/urlretrieve`
默认还会解 `file://`,即可读本地文件或打内网(169.254.169.254 元数据、localhost、内网段)。
本护栏:抓取前校验 —— **只允许 http/https,且解析出的所有 IP 都不能落内网/环回/元数据段**。

best-effort:校验不过就抛 `UnsafeURLError`,调用方 catch 后跳过该 URL(不崩流水线)。
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


class UnsafeURLError(ValueError):
    """URL 未通过 SSRF 护栏(协议不允许 / 指向内网)。"""


def _blocked_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # 解析不出 = 不放行
    return bool(
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    )


def assert_safe_url(url: str) -> str:
    """通过则原样返回 url;否则抛 UnsafeURLError。只允许 http/https + 非内网 IP。"""
    p = urlparse(url or "")
    if p.scheme not in ("http", "https"):
        raise UnsafeURLError(f"scheme not allowed: {p.scheme!r}")
    host = p.hostname
    if not host:
        raise UnsafeURLError("missing host")
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception as e:  # noqa: BLE001
        raise UnsafeURLError(f"dns resolve failed: {e}")
    for info in infos:
        ip = info[4][0]
        if _blocked_ip(ip):
            raise UnsafeURLError(f"resolves to blocked/internal ip: {ip}")
    return url
