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
        if audio is not None and audio != 'none' and not (
            isinstance(audio, str) and audio.lower().startswith('mp4a')
        ):
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
            bool(isinstance(audio, str) and audio.lower().startswith('mp4a')),
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
    if is_tiktok:
        options['impersonate'] = browser_target
        if mode == 'download':
            options['format'] = lambda ctx: select_bounded_mp4(ctx, int(limit))
    with SafeYoutubeDL(options) as downloader:
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
