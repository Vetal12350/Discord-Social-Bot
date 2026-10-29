"""Disposable yt-dlp worker. No cookies, plugins, external tools or private network."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from link_utils import is_supported_url
from safe_http import allowed_url, public_ip


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

    class SafeYoutubeDL(yt_dlp.YoutubeDL):
        def build_request_director(self, handlers, preferences=None):
            return super().build_request_director([UrllibRH], preferences=[])

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
    with SafeYoutubeDL(options) as downloader:
        info = downloader.extract_info(url, download=mode == 'download')
    if not isinstance(info, dict) or info.get('_type') in {'playlist', 'multi_video'}:
        raise ValueError('Only single public videos supported')
    print(json.dumps({k: info.get(k) for k in (
        'description', 'title', 'uploader', 'uploader_id', 'id')}, ensure_ascii=True))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # Providers may include signed URLs or captions in exception text.
        print('Public media unavailable or blocked', file=sys.stderr)
        raise SystemExit(1)
