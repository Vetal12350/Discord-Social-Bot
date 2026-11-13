"""One pre-body TikTok GET/HEAD retry inside the existing worker budgets."""
from email.utils import parsedate_to_datetime
import math
import sys
import time
from urllib.parse import urlsplit

from link_utils import TIKTOK_HOSTS
from safe_http import allowed_url

RETRY_STATUSES = frozenset({403, 429, 500, 502, 503, 504})
MAX_DELAY = 2.0
HTTP_PHASES = frozenset({'short_redirect', 'metadata_get', 'media_get', 'other', 'unknown'})
HTTP_CATEGORIES = frozenset({
    'http_400', 'http_401', 'http_403', 'http_404', 'http_408', 'http_410', 'http_429',
    'http_500', 'http_501', 'http_502', 'http_503', 'http_504',
    'http_4xx', 'http_5xx', 'http_other', 'http_unknown',
})
RETRY_SKIP_REASONS = frozenset({
    'retry_budget_exhausted', 'method_not_safe', 'request_body', 'authorization',
    'url_policy', 'status_not_retryable', 'redirect_loop', 'first_party_403',
    'html_challenge', 'retry_after_not_eligible',
})
MAX_HTTP_DIAGNOSTICS = 8


def http_phase(url):
    """Classify the failing request's role, not an inferred redirect endpoint."""
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or '').lower()
    except ValueError:
        return 'unknown'
    if not host:
        return 'unknown'
    if host in {'vt.tiktok.com', 'vm.tiktok.com'} or (host in TIKTOK_HOSTS and parsed.path.startswith('/t/')):
        return 'short_redirect'
    if host in TIKTOK_HOSTS:
        return 'metadata_get'
    domains = ('tiktok.com', 'tiktokv.com', 'tiktokcdn.com', 'tiktokcdn-us.com',
               'tiktokcdn-eu.com', 'muscdn.com', 'byteoversea.com', 'ibytedtos.com',
               'ibyteimg.com', 'byteimg.com')
    if host == 'offload.tnktok.com' or any(host == d or host.endswith('.' + d) for d in domains):
        return 'media_get'
    return 'other'


def http_category(status):
    category = f'http_{status}'
    if category in HTTP_CATEGORIES:
        return category
    if type(status) is int:
        return 'http_4xx' if 400 <= status < 500 else 'http_5xx' if 500 <= status < 600 else 'http_other'
    return 'http_unknown'


def retry_delay(headers, now=None):
    """Honor short Retry-After values; decline long/invalid waits, never cap them."""
    value = headers.get('Retry-After') or headers.get('retry-after')
    if value is None:
        return 1.0
    try:
        value = str(value).strip()
        if value.isdigit():
            delay = float(value)
        else:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                return None
            delay = math.ceil(date.timestamp() - (time.time() if now is None else now))
        return max(0.25, delay) if math.isfinite(delay) and delay <= MAX_DELAY else None
    except (ValueError, OverflowError, TypeError):
        return None


class BoundedTikTokGet:
    """At most one retry for the entire worker, not one per URL/redirect.

    Only HTTP failures before a response body is returned are eligible. The
    original send function performs all existing URL/DNS/redirect checks again.
    Never retry a POST, protected first-party 403, HTML challenge, body error,
    policy error, 404, or unknown transport failure. No cookies/headers/IP change.
    """

    def __init__(self):
        self.remaining = 1
        self.diagnostics_remaining = MAX_HTTP_DIAGNOSTICS

    def record_failure(self, url, status, skip_reason):
        if self.diagnostics_remaining:
            self.diagnostics_remaining -= 1
            # All values are fixed tokens; never emit a URL, headers, body or exception.
            print(f'media_worker_http_failure phase={http_phase(url)} status={http_category(status)} '
                  f'retry={"skipped" if skip_reason else "scheduled"} '
                  f'retry_reason={skip_reason or "eligible"}', file=sys.stderr)

    def open(self, send, request):
        from yt_dlp.networking.exceptions import HTTPError
        try:
            return send(request)
        except HTTPError as exc:
            url = request if isinstance(request, str) else getattr(request, 'url', '')
            method = 'GET' if isinstance(request, str) else getattr(request, 'method', '')
            data = None if isinstance(request, str) else getattr(request, 'data', None)
            headers = {} if isinstance(request, str) else getattr(request, 'headers', {})
            host = (urlsplit(url).hostname or '').lower()
            response_headers = exc.response.headers
            content_type = str(response_headers.get('Content-Type') or response_headers.get('content-type', '')).lower()
            skip_reason = (
                'retry_budget_exhausted' if not self.remaining else
                'method_not_safe' if method not in {'GET', 'HEAD'} else
                'request_body' if data is not None else
                'authorization' if (headers.get('Authorization') or headers.get('authorization')
                                    or headers.get('Proxy-Authorization')) else
                'url_policy' if not allowed_url(url, media=True) else
                'status_not_retryable' if exc.status not in RETRY_STATUSES else
                'redirect_loop' if exc.redirect_loop else
                'first_party_403' if exc.status == 403 and host in TIKTOK_HOSTS else
                'html_challenge' if exc.status == 403 and 'html' in content_type else None
            )
            delay = retry_delay(response_headers) if skip_reason is None else None
            if skip_reason is None and delay is None:
                skip_reason = 'retry_after_not_eligible'
            self.record_failure(url, exc.status, skip_reason)
            if skip_reason:
                raise
            self.remaining = 0
            phase = http_phase(url)
            exc.close()
            print(f'media_worker_retry reason=http_{exc.status} phase={phase} delay_ms={round(delay * 1000)}',
                  file=sys.stderr)
            time.sleep(delay)  # Disposable child only; parent event loop stays asynchronous.
            try:
                return send(request)  # No recursion, no third request, no body/read retry.
            except HTTPError as final:
                self.record_failure(url, final.status, 'retry_budget_exhausted')
                raise
