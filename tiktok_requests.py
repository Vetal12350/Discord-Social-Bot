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
            if (not self.remaining or method not in {'GET', 'HEAD'} or data is not None
                    or headers.get('Authorization') or headers.get('authorization')
                    or headers.get('Proxy-Authorization') or not allowed_url(url, media=True)
                    or exc.status not in RETRY_STATUSES or exc.redirect_loop):
                raise
            if exc.status == 403 and (host in TIKTOK_HOSTS or 'html' in content_type):
                raise  # A login/CAPTCHA/first-party access refusal is not a retry signal.
            delay = retry_delay(response_headers)
            if delay is None:
                raise
            self.remaining = 0
            phase = ('short_redirect' if host in {'vt.tiktok.com', 'vm.tiktok.com'}
                     else 'metadata_get' if host in TIKTOK_HOSTS else 'media_get')
            exc.close()
            print(f'media_worker_retry reason=http_{exc.status} phase={phase} delay_ms={round(delay * 1000)}',
                  file=sys.stderr)
            time.sleep(delay)  # Disposable child only; parent event loop stays asynchronous.
            return send(request)  # No recursion, no third request, no body/read retry.
