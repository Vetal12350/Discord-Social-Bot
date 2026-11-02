"""Bounded browser-TLS requests for TikTok in the disposable media worker.

curl_cffi uses native libcurl sockets, so Python's socket.connect audit hook
does not protect it. Validate *every* request and redirect, reject non-public
DNS answers, and pin libcurl to the vetted numeric address (CURLOPT_RESOLVE).
This function runs only inside the existing resource-limited child process.
"""
from __future__ import annotations

import socket
from urllib.parse import urljoin, urlsplit

from safe_http import allowed_url, public_ip

MAX_REDIRECTS = 5


def pinned_address(host: str) -> str:
    """Resolve and vet all DNS answers; only a vetted IP reaches libcurl."""
    try:
        answers = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except (OSError, ValueError) as exc:
        raise OSError('Browser media DNS unavailable') from exc
    if not answers:
        raise OSError('Browser media DNS unavailable')

    ip_list = [result[4][0] for result in answers]
    try:
        if not all(public_ip(ip) for ip in ip_list):
            raise OSError('Non-public browser media address rejected')
    except ValueError as exc:
        raise OSError('Invalid browser media DNS answer') from exc

    # Prefer IPv4 when available; curl's CURLOPT_RESOLVE pins this exact IP.
    for ip in ip_list:
        if ':' not in ip:
            return ip
    return '[' + ip_list[0] + ']'


def guarded_curl_request(original_request, session, method, url, *,
                         resolve_option, **kwargs):
    """Only public allowlisted HTTPS via pinned DNS, no native auto-redirects.

    Each redirect is checked separately before a new TCP connection is made.
    All arguments other than the redirection and network guard reach curl_cffi
    unchanged, preserving Chrome impersonation, cookies and streaming.
    """
    if method.upper() not in {'GET', 'HEAD'}:
        raise OSError('Browser media HTTP method rejected')
    if kwargs.get('proxy') or kwargs.get('proxies'):
        raise OSError('Browser media proxies rejected')

    for redirect_number in range(MAX_REDIRECTS + 1):
        if not allowed_url(url, media=True):
            raise OSError('Browser media destination rejected')
        parsed = urlsplit(url)
        host = parsed.hostname
        if not host:
            raise OSError('Browser media destination rejected')
        ip = pinned_address(host)
        # The native request must not re-resolve a hostname between checking
        # DNS and connecting. No proxy or automatic libcurl redirect may bypass
        # the pinning/allowlist above.
        options = dict(kwargs.get('curl_options') or {})
        options[resolve_option] = [f'{host}:443:{ip}']
        request_kwargs = dict(kwargs, allow_redirects=False, max_redirects=0,
                              curl_options=options, proxy='', proxies={})
        response = original_request(session, method, url, **request_kwargs)
        if response.status_code not in {301, 302, 303, 307, 308}:
            return response
        location = response.headers.get('Location')
        response.close()
        if not location or redirect_number == MAX_REDIRECTS:
            raise OSError('Browser media redirect unavailable')
        url = urljoin(url, location)
        if not allowed_url(url, media=True):
            raise OSError('Browser media redirect rejected')
    raise OSError('Too many browser media redirects')


def enable_guarded_browser_requests():
    """Install a child-local curl_cffi request guard (TikTok only)."""
    from curl_cffi import CurlOpt, requests
    if not hasattr(CurlOpt, 'RESOLVE'):
        raise RuntimeError('curl_cffi DNS pinning unsupported')
    original = requests.Session.request

    def guarded(session, method, url, **kwargs):
        return guarded_curl_request(original, session, method, url,
                                    resolve_option=CurlOpt.RESOLVE, **kwargs)

    requests.Session.request = guarded
