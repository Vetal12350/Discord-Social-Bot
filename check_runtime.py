"""Panel-friendly preflight: no token/.env access, downloads, installs or Discord login."""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

PACKAGES = ('discord.py', 'aiohttp', 'python-dotenv', 'yt-dlp', 'langdetect', 'Pillow', 'psutil')


def check() -> int:
    print(f'Python: {sys.version.split()[0]} ({sys.platform}, {platform.machine()})', flush=True)
    environment = {key: os.environ[key] for key in ('SYSTEMROOT', 'WINDIR', 'PATH', 'TEMP', 'TMP', 'LANG', 'LC_ALL')
                   if key in os.environ}
    missing = False
    for package in PACKAGES:
        try:
            print(f'{package}: {version(package)}')
        except PackageNotFoundError:
            print(f'MISSING: {package}; install requirements.txt')
            missing = True
    if missing:
        return 1
    try:
        import bot  # noqa: F401 -- verifies the full runtime import graph, without running main().
        from PIL import features
        if not features.check('webp'):
            print('ERROR: Pillow has no WebP support')
            return 1
        print('Bot imports and Pillow WebP: OK')
    except Exception as exc:
        print(f'ERROR: runtime imports failed ({type(exc).__name__})')
        return 1

    gif_ready = True
    for name in ('ffmpeg', 'ffprobe'):
        suffix = '.exe' if sys.platform == 'win32' else ''
        local = Path(__file__).parent / '.tools' / 'ffmpeg' / 'bin' / (name + suffix)
        executable = str(local) if local.is_file() else shutil.which(name)
        if not executable:
            print(f'MISSING: {name}; GIF conversion is unavailable, originals will be retained')
            gif_ready = False
            continue
        try:
            result = subprocess.run([executable, '-version'], capture_output=True, text=True,
                                    encoding='utf-8', errors='replace', timeout=10, check=True, env=environment)
            print(result.stdout.splitlines()[0])
            if name == 'ffmpeg':
                encoders = subprocess.run([executable, '-hide_banner', '-encoders'], capture_output=True,
                                         text=True, encoding='utf-8', errors='replace', timeout=10, check=True,
                                         env=environment).stdout
                webp = 'libwebp_anim' in encoders
                gif = any(line.split()[1:2] == ['gif'] for line in encoders.splitlines())
                print(f'FFmpeg encoders: libwebp_anim={webp}, gif={gif}')
                if not webp and not gif:
                    gif_ready = False
        except (OSError, subprocess.SubprocessError, IndexError) as exc:
            print(f'ERROR: {name} is not usable ({type(exc).__name__})')
            gif_ready = False
    print('GIF preflight: ' + ('OK' if gif_ready else 'NOT READY'))
    print('Next: set the Startup main file back to bot.py; provide the token privately, then start.')
    return 0 if gif_ready else 2


if __name__ == '__main__':
    raise SystemExit(check())
