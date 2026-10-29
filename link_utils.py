"""Parse only supported social-media links and make Discord-friendly links."""
from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

URL_RE = re.compile(r"https?://[^\s<>\[\]{}]+", re.IGNORECASE)
TRAILING_PUNCTUATION = ".,!?;:'\"»”)]}"
X_HOSTS = {"x.com", "www.x.com", "mobile.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com",
           "fixupx.com", "www.fixupx.com", "fxtwitter.com", "www.fxtwitter.com"}
TIKTOK_HOSTS = {"tiktok.com", "www.tiktok.com", "m.tiktok.com", "vm.tiktok.com", "vt.tiktok.com"}


def extract_urls(message: str, limit: int = 3) -> list[str]:
    """Find up to limit validated source URLs, without repeats."""
    results: list[str] = []
    if limit <= 0:
        return results
    for match in URL_RE.finditer(message):
        url = match.group(0).rstrip(TRAILING_PUNCTUATION)
        if is_supported_url(url):
            url = canonical_url(url)
        if is_supported_url(url) and url not in results:
            results.append(url)
            if len(results) >= limit:
                break
    return results


def is_supported_url(url: str) -> bool:
    if len(url) > 2048 or re.search(r'[\s\\\x00-\x1f\x7f]', url):
        return False
    try:
        p = urlsplit(url)
        host = (p.hostname or "").lower()
        if p.scheme != "https" or p.username or p.password or p.port:
            return False
        if host in X_HOSTS:
            return bool(re.fullmatch(r"/(?:[A-Za-z0-9_]{1,50}|i/web)/status/[0-9]{1,20}(?:/(?:video|photo)/[1-9])?/?", p.path, re.IGNORECASE))
        if host in TIKTOK_HOSTS:
            if host in {"vm.tiktok.com", "vt.tiktok.com"}:
                return bool(re.fullmatch(r"/[A-Za-z0-9_-]+/?", p.path))
            return bool(re.fullmatch(r"/@[A-Za-z0-9_.-]{1,100}/(?:video|photo)/[0-9]{1,20}/?", p.path, re.IGNORECASE)
                        or re.fullmatch(r"/t/[A-Za-z0-9_-]+/?", p.path))
        return False
    except ValueError:
        return False


def canonical_url(url: str) -> str:
    """Drop tracking and collapse X aliases/media suffixes for deduplication."""
    if not is_supported_url(url):
        raise ValueError('Unsupported social link')
    p = urlsplit(url)
    if p.hostname.lower() in X_HOSTS:
        post_id = re.search(r'/status/([0-9]+)', p.path, re.I).group(1)
        return f'https://x.com/i/status/{post_id}'
    return urlunsplit(('https', p.hostname.lower(), p.path.rstrip('/'), '', ''))


def to_embed_url(url: str) -> str:
    """Switch a validated social URL to a public embed proxy; discard tracking params."""
    if not is_supported_url(url):
        raise ValueError("Unsupported or malformed social link")
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    if host in X_HOSTS:
        proxy = "fixupx.com"
    elif host == "vm.tiktok.com":
        proxy = "vm.tnktok.com"
    elif host == "vt.tiktok.com":
        proxy = "vt.tnktok.com"
    else:
        proxy = "tnktok.com"
    return urlunsplit(("https", proxy, p.path, "", ""))
