"""Cancellable worker with bounded runtime, disk usage and output."""
from __future__ import annotations

import asyncio
from importlib.util import find_spec
import json
import logging
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urljoin

import aiohttp
import psutil
from link_utils import source_log_key
from safe_http import allowed_url
from media_cache import MediaCache
from process_limits import RSS_LIMIT, limit_process, under_pressure
from tiktok_requests import HTTP_CATEGORIES, HTTP_PHASES, MAX_HTTP_DIAGNOSTICS, RETRY_SKIP_REASONS

log = logging.getLogger(__name__)
MAX_BYTES = 19 * 1024 * 1024
worker_slot = asyncio.Semaphore(1)

# Never log raw child stderr: yt-dlp and provider errors may include signed URLs.
SAFE_MEDIA_WORKER_REASONS = frozenset({
    'format_unavailable', 'http_403', 'http_429', 'access_challenge',
    'extractor_failed', 'network_timeout', 'network_rejected',
    'network_or_policy_error', 'worker_error', 'no_mp4_output',
})


def safe_worker_failure(stderr_path: Path) -> str:
    """Read only our fixed allowlisted diagnostics, never provider text."""
    try:
        for line in stderr_path.read_text(encoding='utf-8', errors='replace').splitlines()[-8:]:
            if line.startswith('media_worker_reason='):
                reason = line.removeprefix('media_worker_reason=').strip()
                if reason in SAFE_MEDIA_WORKER_REASONS:
                    return reason
    except OSError:
        pass
    return 'unclassified'


def safe_worker_retry(stderr_path: Path):
    """Read only our bounded retry marker, never emit arbitrary child stderr."""
    try:
        with stderr_path.open('rb') as source:
            lines = source.read(65536).decode('utf-8', errors='replace').splitlines()
    except OSError:
        return None
    for line in lines:
        match = re.fullmatch(r'media_worker_retry reason=(http_(?:403|429|500|502|503|504)) '
                             r'phase=(short_redirect|metadata_get|media_get|other|unknown) delay_ms=([0-9]{1,4})', line)
        if match and 250 <= int(match[3]) <= 2000:
            return match[1], match[2], int(match[3])
    return None


def safe_worker_http_failures(stderr_path: Path):
    """Accept bounded fixed diagnostics even when retry is forbidden/not used."""
    try:
        with stderr_path.open('rb') as source:
            lines = source.read(65536).decode('utf-8', errors='replace').splitlines()
    except OSError:
        return []
    results = []
    for line in lines:
        match = re.fullmatch(r'media_worker_http_failure phase=([a-z_]+) status=([a-z_0-9]+) '
                             r'retry=(scheduled|skipped) retry_reason=([a-z_0-9]+)', line)
        if not match:
            continue
        phase, status, retry, reason = match.groups()
        if (phase in HTTP_PHASES and status in HTTP_CATEGORIES
                and ((retry == 'scheduled' and reason == 'eligible')
                     or (retry == 'skipped' and reason in RETRY_SKIP_REASONS))):
            results.append((phase, status, retry, reason))
            if len(results) == MAX_HTTP_DIAGNOSTICS:
                break
    return results


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
        for package in ('psutil', 'PIL', 'aiohttp', 'yt_dlp', 'curl_cffi'):
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


def parse_worker_output(stdout_path: Path, *, download: bool) -> dict:
    """Require JSON for metadata, not for an already verified media artifact.

    Successful download workers may produce empty/unparseable stdout while
    still writing a valid MP4. Callers MUST verify the file's existence,
    size and codecs before sending it; metadata mode always requires JSON.
    """
    if download:
        return {}  # Download completion is confirmed by the MP4 validation.
    try:
        result = json.loads(stdout_path.read_text(encoding='utf-8'))
    except (ValueError, UnicodeError) as exc:
        log.info('media_worker_output_invalid mode=metadata reason=%s',
                 type(exc).__name__)
        raise RuntimeError('Media worker returned invalid metadata') from None
    if not isinstance(result, dict):
        raise RuntimeError('Media worker returned invalid metadata')
    return result


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
                    log.info('media_download_failure kind=image status=%d phase=response', response.status)
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
                    log.info('media_download_failure kind=mp4 status=%d phase=response', response.status)
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
        log.info('media_worker_guard source_key=%s mode=%s reason=memory_pressure reserve_bytes=%d',
                 source_log_key(url), 'download' if download else 'metadata',
                 RSS_LIMIT + (min(limit, MAX_BYTES) if download else 0))
        raise RuntimeError('Media worker skipped under memory pressure')
    async with worker_slot:
        return await _run_media(url, directory, download=download, limit=limit)


async def _run_media(url: str, directory: Path, *, download: bool,
                     limit: int) -> dict:
    # The child needs installed packages but must never inherit Discord secrets.
    environment = child_environment(python=True, network=True)
    stdout_path = directory / 'result.json'
    stderr_path = directory / 'worker.log'
    started = time.monotonic()
    size = rss = 0
    cpu_seconds = 0.0

    def guard(reason):
        # Only fixed reasons, measured numbers and a validated public post key.
        # Do not read/log raw stderr or provider exception messages.
        log.info('media_worker_guard source_key=%s mode=%s reason=%s elapsed_ms=%d '
                 'directory_bytes=%d upload_limit=%d stdout_bytes=%d stderr_bytes=%d '
                 'rss_bytes=%d rss_limit=%d cpu_seconds=%.3f cpu_limit=8',
                 source_log_key(url), 'download' if download else 'metadata', reason,
                 round((time.monotonic() - started) * 1000), size, limit,
                 stdout_path.stat().st_size, stderr_path.stat().st_size,
                 rss, RSS_LIMIT, cpu_seconds)

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
                    if size > limit + 512 * 1024:
                        guard('directory_limit')
                        raise RuntimeError('Media size limit exceeded')
                    if stdout_path.stat().st_size > 1024 * 1024:
                        guard('stdout_limit')
                        raise RuntimeError('Media size limit exceeded')
                    if stderr_path.stat().st_size > 65536:
                        guard('stderr_limit')
                        raise RuntimeError('Media size limit exceeded')
                    if stats:
                        try:
                            cpu = stats.cpu_times()
                            rss = stats.memory_info().rss
                            cpu_seconds = cpu.user + cpu.system
                            if rss > RSS_LIMIT:
                                guard('rss_limit')
                                raise RuntimeError('Media worker resource limit exceeded')
                            if cpu_seconds > 8:
                                guard('cpu_limit')
                                raise RuntimeError('Media worker resource limit exceeded')
                        except psutil.NoSuchProcess:
                            pass
                    try:
                        await asyncio.wait_for(process.wait(), timeout=0.1)
                    except TimeoutError:
                        pass
                if process.returncode:
                    reason = safe_worker_failure(stderr_path)
                    log.info('media_worker_failure mode=%s reason=%s exit_kind=%s source_key=%s returncode=%d',
                             'download' if download else 'metadata', reason,
                             'signal' if process.returncode < 0 else 'error',
                             source_log_key(url), process.returncode)
                    raise RuntimeError('Public media unavailable or blocked')
        except TimeoutError:
            guard('wall_timeout')
            raise
        finally:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
            if close_job:
                close_job()
            for failure in safe_worker_http_failures(stderr_path):
                log.info('media_worker_http_failure source_key=%s mode=%s phase=%s status=%s '
                         'retry=%s retry_reason=%s',
                         source_log_key(url), 'download' if download else 'metadata', *failure)
            retry = safe_worker_retry(stderr_path)
            if retry:
                log.info('media_worker_retry source_key=%s mode=%s reason=%s phase=%s delay_ms=%d',
                         source_log_key(url), 'download' if download else 'metadata', *retry)
    if stdout_path.stat().st_size > 1024 * 1024:
        guard('stdout_limit')
        raise RuntimeError('Metadata too large')
    return parse_worker_output(stdout_path, download=download)
