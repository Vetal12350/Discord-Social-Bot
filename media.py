"""Cancellable worker with bounded runtime, disk usage and output."""
from __future__ import annotations

import asyncio
from importlib.util import find_spec
import json
import os
from pathlib import Path
import sys
from urllib.parse import urljoin

import aiohttp
import psutil
from safe_http import allowed_url
from media_cache import MediaCache
from process_limits import RSS_LIMIT, limit_process, under_pressure

MAX_BYTES = 19 * 1024 * 1024
worker_slot = asyncio.Semaphore(1)


def child_environment(*, python: bool = False, network: bool = False) -> dict[str, str]:
    """Give disposable tools no secrets, but let Python find installed packages.

    Altare installs dependencies under ~/.local/.../site-packages.  The parent
    can import them via its startup path; a fresh Python child with a stripped
    environment may not.  Derive package roots from the *installed modules*,
    never from the inherited PYTHONPATH or arbitrary user-controlled paths.
    """
    allowed = {'SYSTEMROOT', 'WINDIR', 'PATH', 'TEMP', 'TMP', 'LANG', 'LC_ALL'}
    if network:
        allowed.update({'SSL_CERT_FILE', 'SSL_CERT_DIR'})
    environment = {key: value for key, value in os.environ.items()
                   if key.upper() in allowed}
    if python:
        roots = []
        for package in ('psutil', 'PIL', 'aiohttp', 'yt_dlp'):
            spec = find_spec(package)
            if spec is None or not spec.origin:
                continue
            location = Path(spec.origin).resolve()
            root = location.parent.parent if location.name == '__init__.py' else location.parent
            if root.is_dir() and str(root) not in roots:
                roots.append(str(root))
        if roots:
            environment['PYTHONPATH'] = os.pathsep.join(roots)
    return environment


class MediaTooLarge(RuntimeError):
    pass


async def download_image(session, url: str, destination: Path, limit: int) -> Path:
    """Bounded image download; no decoding, provider filenames or HTML attachments."""
    async with asyncio.timeout(60):
        for _ in range(4):
            if not allowed_url(url, media=True):
                raise RuntimeError('Image destination rejected')
            async with session.get(url, allow_redirects=False,
                                   timeout=aiohttp.ClientTimeout(total=55)) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    url = urljoin(url, response.headers.get('Location', ''))
                    continue
                if response.status != 200:
                    raise RuntimeError('Image unavailable')
                if response.content_length and response.content_length > limit:
                    raise MediaTooLarge('Image exceeds remaining upload budget')
                size = 0
                signature = bytearray()
                with destination.open('wb') as output:
                    async for chunk in response.content.iter_chunked(65536):
                        size += len(chunk)
                        if size > limit:
                            raise MediaTooLarge('Image exceeds remaining upload budget')
                        signature.extend(chunk[:max(0, 16 - len(signature))])
                        await MediaCache.disk(output.write, chunk)
                if signature.startswith(b'\xff\xd8\xff'):
                    extension = '.jpg'
                elif signature.startswith(b'\x89PNG\r\n\x1a\n'):
                    extension = '.png'
                elif signature.startswith((b'GIF87a', b'GIF89a')):
                    extension = '.gif'
                elif signature[:4] == b'RIFF' and signature[8:12] == b'WEBP':
                    extension = '.webp'
                else:
                    raise ValueError('Unsupported image or HTML instead of media')
                result = destination.with_suffix(extension)
                destination.rename(result)
                return result
        raise RuntimeError('Too many image redirects')


async def download_mp4(session, url: str, destination: Path, limit: int):
    """Stream an approved provider MP4, bounded even without Content-Length."""
    async with asyncio.timeout(60):
        for _ in range(4):
            if not allowed_url(url, media=True):
                raise RuntimeError('Media destination rejected')
            async with session.get(url, allow_redirects=False,
                                   timeout=aiohttp.ClientTimeout(total=55)) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    url = urljoin(url, response.headers.get('Location', ''))
                    continue
                if response.status != 200:
                    raise RuntimeError('Media unavailable')
                if response.content_length and response.content_length > limit:
                    raise MediaTooLarge('Media size limit exceeded')
                size = 0
                signature = bytearray()
                with destination.open('wb') as output:
                    async for chunk in response.content.iter_chunked(65536):
                        size += len(chunk)
                        if size > limit:
                            raise MediaTooLarge('Media size limit exceeded')
                        if len(signature) < 32:
                            signature.extend(chunk[:32 - len(signature)])
                        await MediaCache.disk(output.write, chunk)
                if size < 12 or signature[4:8] != b'ftyp':
                    raise RuntimeError('Provider did not return an MP4')
                return
        raise RuntimeError('Too many media redirects')


async def run_media(url: str, directory: Path, *, download: bool,
                    limit: int = MAX_BYTES) -> dict:
    if under_pressure(reserve=RSS_LIMIT + (min(limit, MAX_BYTES) if download else 0)):
        raise RuntimeError('Media worker skipped under memory pressure')
    async with worker_slot:
        return await _run_media(url, directory, download=download, limit=limit)


async def _run_media(url: str, directory: Path, *, download: bool,
                     limit: int) -> dict:
    # The child needs installed packages but must never inherit Discord secrets.
    environment = child_environment(python=True, network=True)
    stdout_path = directory / 'result.json'
    stderr_path = directory / 'worker.log'
    with stdout_path.open('wb') as stdout, stderr_path.open('wb') as stderr:
        process = await asyncio.create_subprocess_exec(
            sys.executable, str(Path(__file__).with_name('media_worker.py')),
            'download' if download else 'metadata', url, str(directory), str(limit),
            stdout=stdout, stderr=stderr, env=environment,
            creationflags=0x08000000 | 0x4000 if os.name == 'nt' else 0,
        )
        close_job = None
        try:
            try:
                stats, close_job = limit_process(process.pid)
            except psutil.NoSuchProcess:
                stats = None
            async with asyncio.timeout(90 if download else 25):
                while process.returncode is None:
                    # Worker writes only one stream; allow one network buffer beyond limit.
                    size = sum(p.stat().st_size for p in directory.iterdir() if p.is_file())
                    if (size > limit + 512 * 1024 or stdout_path.stat().st_size > 1024 * 1024
                            or stderr_path.stat().st_size > 65536):
                        raise RuntimeError('Media size limit exceeded')
                    if stats:
                        try:
                            cpu = stats.cpu_times()
                            if stats.memory_info().rss > RSS_LIMIT or cpu.user + cpu.system > 8:
                                raise RuntimeError('Media worker resource limit exceeded')
                        except psutil.NoSuchProcess:
                            pass
                    try:
                        await asyncio.wait_for(process.wait(), timeout=0.1)
                    except TimeoutError:
                        pass
                if process.returncode:
                    raise RuntimeError('Public media unavailable or blocked')
        finally:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
            if close_job:
                close_job()
    if stdout_path.stat().st_size > 1024 * 1024:
        raise RuntimeError('Metadata too large')
    data = json.loads(stdout_path.read_text(encoding='utf-8'))
    return data if isinstance(data, dict) else {}
