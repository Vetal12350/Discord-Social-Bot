"""Disposable yt-dlp worker. No cookies, plugins, external tools or private network."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

from link_utils import TIKTOK_HOSTS, is_supported_url
from safe_http import allowed_url, public_ip


class NoMP4OutputError(RuntimeError):
    """yt-dlp exited successfully without writing an actual MP4."""


def ensure_mp4_output(folder: str) -> None:
    """Do not report success for a size-skipped or missing download."""
    output = Path(folder) / 'video.mp4'
    if not output.is_file() or output.stat().st_size == 0:
        raise NoMP4OutputError('No MP4 file produced')


def is_aac_codec(value: object) -> bool:
    """yt-dlp labels AAC as either 'aac' or an MP4 sample entry 'mp4a.*'."""
    return isinstance(value, str) and (
        value.lower() == 'aac' or value.lower().startswith('mp4a')
    )


def select_bounded_mp4(ctx: dict, limit: int):
    """Select one progressive H.264 MP4 whose reported size fits our cap.

    yt-dlp's max_filesize may silently skip an oversized best format and
    return success. Prefer the best *fitting* MP4 instead. Unknown-size
    formats are a last resort, with the existing hard byte/time/RSS limits.
    No DASH/HLS, separate audio, mergers, or external programs are allowed.
    """
    fitting, unknown = [], []
    for fmt in ctx.get('formats', ()):
        if not isinstance(fmt, dict) or fmt.get('ext') != 'mp4' or fmt.get('protocol') != 'https':
            continue
        video = fmt.get('vcodec')
        audio = fmt.get('acodec')
        if not isinstance(video, str) or not video.lower().startswith(('avc1', 'avc3', 'h264')):
            continue
        if audio is not None and audio != 'none' and not is_aac_codec(audio):
            continue

        exact = fmt.get('filesize')
        approx = fmt.get('filesize_approx')
        if type(exact) in (int, float) and 0 < exact < float('inf'):
            if exact <= limit:
                fitting.append(fmt)
            continue
        if type(approx) in (int, float) and 0 < approx < float('inf'):
            # An approximate size can be optimistic. Reserve 10% headroom.
            if approx <= limit * 0.9:
                fitting.append(fmt)
            continue
        unknown.append(fmt)

    def quality(fmt):
        audio = fmt.get('acodec')
        return (
            is_aac_codec(audio),
            fmt.get('height') if type(fmt.get('height')) in (int, float) else 0,
            fmt.get('width') if type(fmt.get('width')) in (int, float) else 0,
            fmt.get('tbr') if type(fmt.get('tbr')) in (int, float) else 0,
        )

    if fitting:
        yield max(fitting, key=quality)
    elif unknown:
        # Choose a moderate format to reduce risk of a size-skip when TikTok
        # does not publish any size information.
        moderate = [fmt for fmt in unknown
                    if type(fmt.get('height')) in (int, float) and fmt['height'] <= 720]
        yield max(moderate or unknown, key=quality)


MAX_VIDEO_MIRRORS = 3  # Chosen CDN copy plus at most two copies of the same format.
MIRROR_STATUSES = ('http error 403', 'http error 404')


def same_format_mirrors(formats, primary) -> list:
    """Other CDN copies of exactly the chosen format, in yt-dlp's order.

    TikTok's PlayAddr.UrlList yields one yt-dlp format per mirror URL with
    identical codec/size/dimensions. Never substitute a different quality,
    codec or size: that would bypass the size-aware selection above.
    """
    keys = ('ext', 'protocol', 'vcodec', 'acodec', 'width', 'height', 'filesize', 'filesize_approx')
    identity = tuple(primary.get(key) for key in keys)
    seen = {primary.get('url')}
    mirrors = []
    for fmt in formats:
        if not isinstance(fmt, dict) or tuple(fmt.get(key) for key in keys) != identity:
            continue
        url = fmt.get('url')
        if not isinstance(url, str) or url in seen:
            continue
        seen.add(url)
        mirrors.append(fmt)
    return mirrors


class MirrorPlan:
    """Format selector yielding the n-th CDN copy of the bounded MP4 choice."""

    def __init__(self, limit: int):
        self.limit = limit
        self.attempt = 0
        self.available = 0

    def candidates(self, ctx: dict) -> list:
        primary = next(select_bounded_mp4(ctx, self.limit), None)
        if primary is None:
            return []
        mirrors = same_format_mirrors(ctx.get('formats', ()), primary)
        return [primary, *mirrors][:MAX_VIDEO_MIRRORS]

    def __call__(self, ctx: dict):
        candidates = self.candidates(ctx)
        self.available = len(candidates)
        if self.attempt < len(candidates):
            yield candidates[self.attempt]


def is_mirror_failure(exc: Exception) -> bool:
    """Only a missing/forbidden media copy justifies trying another copy."""
    return type(exc).__name__ == 'DownloadError' and any(
        status in str(exc).lower() for status in MIRROR_STATUSES)


def download_with_mirrors(downloader, info: dict, plan: MirrorPlan, folder: str) -> dict:
    """Download the already-extracted video, trying same-format CDN copies.

    No metadata re-extraction and no second worker: every attempt stays inside
    this process's existing CPU/RSS/wall/disk limits. Partial files from a
    failed copy are removed so a different URL is never resumed into them.
    """
    import copy
    attempt = 0
    while True:
        plan.attempt = attempt
        for leftover in Path(folder).glob('video.*'):
            leftover.unlink(missing_ok=True)
        try:
            result = downloader.process_ie_result(copy.deepcopy(info), download=True)
        except Exception as exc:  # noqa: BLE001 - re-raised unless a mirror applies
            if not is_mirror_failure(exc) or attempt + 1 >= plan.available:
                raise
            attempt += 1
            continue
        if attempt:
            print(f'media_worker_mirror candidate={attempt + 1} of={plan.available}', file=sys.stderr)
        return result


def network_guard(event, args):
    # urllib emits this for initial requests AND every redirect.
    if event == 'urllib.Request' and not allowed_url(args[0], media=True):
        raise OSError('Network destination is not allowed')
    # Check the actual numeric destination, avoiding DNS rebinding.
    if event == 'socket.connect' and not public_ip(args[1][0]):
        raise OSError('Non-public network destination rejected')
    if event in {'subprocess.Popen', 'os.system', 'os.exec', 'os.posix_spawn'}:
        raise OSError('External programs disabled in media worker')


def main():
    mode, url, folder, limit = sys.argv[1:]
    if mode not in {'metadata', 'download'} or not is_supported_url(url):
        raise ValueError('Invalid media request')
    # Explicitly load built-in extractors only; never discover third-party plugins.
    import yt_dlp
    from yt_dlp.globals import plugin_dirs
    from yt_dlp.networking._urllib import UrllibRH

    plugin_dirs.value = []
    is_tiktok = urlsplit(url).hostname in TIKTOK_HOSTS

    if is_tiktok:
        # The Python audit hook cannot see native libcurl socket connections.
        # Explicitly pin each resolved public IP and validate every redirect.
        from browser_http import enable_guarded_browser_requests
        enable_guarded_browser_requests()
        from yt_dlp.networking._curlcffi import CurlCFFIRH
        from yt_dlp.networking.impersonate import ImpersonateTarget
        browser_target = ImpersonateTarget.from_str('chrome')

    class SafeYoutubeDL(yt_dlp.YoutubeDL):
        def __init__(self, *args, **kwargs):
            if is_tiktok:
                from tiktok_requests import BoundedTikTokGet
                self.tiktok_get_retry = BoundedTikTokGet()
            super().__init__(*args, **kwargs)

        def urlopen(self, request):
            if is_tiktok:
                return self.tiktok_get_retry.open(super().urlopen, request)
            return super().urlopen(request)

        def build_request_director(self, handlers, preferences=None):
            # Keep the original urllib-only policy for X. TikTok can use
            # browser TLS, with native requests guarded in browser_http.
            safe_handlers = [UrllibRH, CurlCFFIRH] if is_tiktok else [UrllibRH]
            return super().build_request_director(safe_handlers, preferences=[])

    sys.addaudithook(network_guard)
    options = {
        'quiet': True, 'no_warnings': True, 'noplaylist': True,
        'socket_timeout': 8, 'retries': 0, 'extractor_retries': 0,
        'fragment_retries': 0, 'proxy': '', 'cachedir': False,
        'skip_download': mode == 'metadata', 'max_filesize': int(limit),
        # One progressive MP4 only: no ffmpeg, merges, playlists or HLS segments.
        'format': ('b[ext=mp4][protocol=https][vcodec^=avc1][acodec^=mp4a]/'
                   'b[ext=mp4][protocol=https][vcodec^=h264][acodec^=mp4a]/'
                   'b[ext=mp4][protocol=https][vcodec^=avc1]/b[ext=mp4][protocol=https][vcodec^=h264]'
                   if mode == 'download' else 'best'),
        'outtmpl': str(Path(folder) / 'video.%(ext)s'),
        'overwrites': False, 'writethumbnail': False, 'writeinfojson': False,
        'postprocessors': [], 'extract_flat': False,
    }
    plan = None
    if is_tiktok:
        options['impersonate'] = browser_target
        if mode == 'download':
            plan = MirrorPlan(int(limit))
            options['format'] = plan
    with SafeYoutubeDL(options) as downloader:
        if plan is not None:
            info = downloader.extract_info(url, download=False)
            if isinstance(info, dict) and info.get('_type') not in {'playlist', 'multi_video'}:
                info = download_with_mirrors(downloader, info, plan, folder)
        else:
            info = downloader.extract_info(url, download=mode == 'download')
    if not isinstance(info, dict) or info.get('_type') in {'playlist', 'multi_video'}:
        raise ValueError('Only single public videos supported')
    if mode == 'download':
        ensure_mp4_output(folder)
    print(json.dumps({k: info.get(k) for k in (
        'description', 'title', 'uploader', 'uploader_id', 'id')}, ensure_ascii=True))


def safe_failure_reason(exc: Exception) -> str:
    """Bounded public error category, never raw yt-dlp text or media URLs."""
    kind = type(exc).__name__
    if kind == 'NoMP4OutputError':
        return 'no_mp4_output'
    if kind == 'DownloadError':
        # Do not emit the error message: it can contain signed media URLs.
        message = str(exc).lower()
        if 'requested format is not available' in message or 'format is not available' in message:
            return 'format_unavailable'
        if 'http error 403' in message or '403 forbidden' in message:
            return 'http_403'
        if 'http error 429' in message or 'too many requests' in message:
            return 'http_429'
        if 'login' in message or 'captcha' in message or 'challenge' in message:
            return 'access_challenge'
        return 'extractor_failed'
    if kind in {'TimeoutError', 'ConnectTimeout', 'ReadTimeout'}:
        return 'network_timeout'
    if kind in {'HTTPError', 'URLError'}:
        return 'network_rejected'
    if kind in {'OSError', 'PermissionError'}:
        return 'network_or_policy_error'
    return 'worker_error'


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Providers may include signed URLs or captions in exception text.
        # Emit only a fixed allowlisted diagnostic token, never exception text.
        print('media_worker_reason=' + safe_failure_reason(exc), file=sys.stderr)
        raise SystemExit(1)
