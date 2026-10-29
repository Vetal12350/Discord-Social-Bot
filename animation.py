"""Bounded local MP4 -> animated WebP/GIF conversion for explicitly tagged X GIFs."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import struct
import sys
import time
from fractions import Fraction

import psutil
from process_limits import RSS_LIMIT, under_pressure, windows_job
from media import child_environment, worker_slot
from media_cache import MediaCache

MAX_SECONDS = 30
MAX_SIDE = 480
MAX_FPS = 12
MAX_FRAMES = MAX_SECONDS * MAX_FPS + 2
CPU_SECONDS = 8
TOTAL_WALL_SECONDS = 60
TOTAL_CPU_SECONDS = 10
STDERR_LIMIT = 65536
log = logging.getLogger(__name__)


class AnimationError(RuntimeError):
    def __init__(self, message: str, *, reason='conversion_failed', details=None):
        super().__init__(message)
        self.reason = reason
        self.details = details or {}


def binary(name: str) -> str:
    local = Path(__file__).parent / '.tools' / 'ffmpeg' / 'bin' / (name + ('.exe' if os.name == 'nt' else ''))
    result = str(local) if local.is_file() else shutil.which(name)
    if not result:
        raise AnimationError('GIF conversion requires FFmpeg and ffprobe')
    return result


async def run_tool(command: list[str], destination: Path, limit: int, *, timeout=35, cpu_limit=CPU_SECONDS):
    """Copy stdout with an exact disk cap; reap on timeout/cancel/CPU/RAM/size failure."""
    # FFmpeg gets only OS essentials; the Python validator additionally gets
    # the installed package roots, not tokens or the inherited PYTHONPATH.
    environment = child_environment(python=command[0] == sys.executable)
    flags = 0x08000000 | 0x4000 if os.name == 'nt' else 0
    started = time.monotonic()
    process = await asyncio.create_subprocess_exec(*command, stdin=asyncio.subprocess.DEVNULL,
                                                 stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                 env=environment, creationflags=flags)
    close_job = None
    tasks = []
    stderr_tail = bytearray()
    stderr_bytes = written = seen = peak_rss = 0
    cpu_seconds = 0.0
    failure = None
    try:
        try:
            stats = psutil.Process(process.pid)
            if os.name == 'nt':
                close_job = windows_job(process.pid, cpu_limit)
            else:
                stats.nice(10)
                stats.rlimit(psutil.RLIMIT_AS, (512 * 1024 * 1024,) * 2)
                stats.rlimit(psutil.RLIMIT_DATA, (128 * 1024 * 1024,) * 2)
                stats.rlimit(psutil.RLIMIT_CPU, (math.ceil(cpu_limit),) * 2)
        except (psutil.NoSuchProcess, ProcessLookupError):
            stats = None

        async def copy():
            nonlocal written, seen
            with destination.open('wb') as output:
                while chunk := await process.stdout.read(65536):
                    seen += len(chunk)
                    if seen > limit:
                        raise AnimationError('Animation output exceeds upload budget', reason='output_limit')
                    await MediaCache.disk(output.write, chunk)
                    written += len(chunk)

        async def capture_stderr():
            nonlocal stderr_bytes
            while chunk := await process.stderr.read(4096):
                stderr_bytes += len(chunk)
                stderr_tail.extend(chunk)
                del stderr_tail[:-8192]
                if stderr_bytes > STDERR_LIMIT:
                    raise AnimationError('FFmpeg stderr limit exceeded', reason='stderr_limit')

        async def monitor():
            nonlocal peak_rss, cpu_seconds
            while process.returncode is None:
                if stats is not None:
                    try:
                        cpu = stats.cpu_times()
                        peak_rss = max(peak_rss, stats.memory_info().rss)
                        cpu_seconds = max(cpu_seconds, cpu.user + cpu.system)
                        if peak_rss > RSS_LIMIT:
                            raise AnimationError('Animation memory limit exceeded', reason='memory_limit')
                        if cpu_seconds > cpu_limit:
                            raise AnimationError('Animation CPU limit exceeded', reason='cpu_limit')
                    except psutil.NoSuchProcess:
                        pass
                await asyncio.sleep(0.05)

        tasks = [asyncio.create_task(copy()), asyncio.create_task(capture_stderr()),
                 asyncio.create_task(monitor()), asyncio.create_task(process.wait())]
        async with asyncio.timeout(timeout):
            await asyncio.gather(*tasks)
        if process.returncode:
            raise AnimationError('FFmpeg/validation process failed', reason='process_failed')
    except AnimationError as exc:
        failure = exc
        raise
    except asyncio.CancelledError:
        failure = AnimationError('Animation process cancelled', reason='cancelled')
        raise
    except TimeoutError as exc:
        failure = AnimationError('Animation process timeout', reason='timeout')
        raise failure from exc
    except (OSError, psutil.Error) as exc:
        failure = AnimationError('Animation process/setup error', reason='process_error')
        raise failure from exc
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if close_job:
            close_job()
        stderr = stderr_tail.decode('utf-8', errors='replace').replace(str(destination.parent), '<temp>')
        stderr = re.sub(r'https?://\S+', '<url>', stderr)
        details = {'returncode': process.returncode, 'stderr': stderr, 'bytes_written': written,
                   'bytes_seen': seen, 'peak_rss': peak_rss, 'cpu_seconds': round(cpu_seconds, 3),
                   'wall_seconds': round(time.monotonic() - started, 3)}
        if failure:
            failure.details = details
        log.info('animation_tool result=%s reason=%s returncode=%s bytes=%s peak_rss=%s cpu=%s wall=%s stderr=%r',
                 'failed' if failure else 'finished', failure.reason if failure else 'completed',
                 process.returncode, written, peak_rss, details['cpu_seconds'], details['wall_seconds'], stderr)
    return details


@dataclass(frozen=True)
class SourceInfo:
    duration: float
    width: int
    height: int
    frame_count: int = 0
    frame_rate: float = 0.0

    @property
    def preserve_short_frames(self) -> bool:
        # Only affects explicit GIF conversion. Never reclassifies an ordinary video.
        frames = self.frame_count or math.ceil(self.duration * self.frame_rate)
        return self.duration < 0.25 and 2 <= frames <= 8 and 0 < self.frame_rate <= 60


def source_info(data: dict) -> SourceInfo:
    try:
        stream = data['streams'][0]
        width, height = int(stream['width']), int(stream['height'])
        duration_value = stream.get('duration')
        if duration_value in {None, '', 'N/A'}:
            duration_value = data['format']['duration']
        duration = float(duration_value)
        sar = stream.get('sample_aspect_ratio') or '1:1'
        if sar in {'N/A', '0:1'}:
            sar = '1:1'
        numerator, denominator = map(int, sar.split(':'))
        ratio = numerator / denominator
        rotation = next((int(float(s['rotation'])) for s in stream.get('side_data_list', []) if 'rotation' in s), 0)
        count = stream.get('nb_frames')
        frame_count = int(count) if str(count).isdigit() else 0
        rate = stream.get('avg_frame_rate') or stream.get('r_frame_rate') or '0/1'
        try:
            frame_rate = float(Fraction(rate))
        except (ValueError, ZeroDivisionError):
            frame_rate = 0.0
    except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError, StopIteration) as exc:
        raise AnimationError('Invalid source dimensions/duration') from exc
    if (not math.isfinite(duration) or not 0 < duration <= MAX_SECONDS
            or not 0 < width <= 4096 or not 0 < height <= 4096 or width * height > 4096 * 2160
            or not math.isfinite(ratio) or not 0.1 <= ratio <= 10):
        raise AnimationError('Source exceeds animation limits; no truncation performed')
    if rotation % 90:
        raise AnimationError('Unsupported display rotation')
    display_width, display_height = width * ratio, height
    if rotation % 180:
        display_width, display_height = display_height, display_width
    return SourceInfo(duration, max(1, round(display_width)), max(1, round(display_height)), frame_count, frame_rate)


def target_size(info: SourceInfo, side: int) -> tuple[int, int]:
    factor = min(1, side / info.width, side / info.height)
    return max(1, round(info.width * factor)), max(1, round(info.height * factor))


def inspect_animation(path: Path, expected_duration: float, expected_width: int,
                      expected_height: int, limit: int) -> dict:
    """Actually decode every bounded frame, checking loop, timing, size and frame changes."""
    from PIL import Image
    if not 0 < path.stat().st_size <= limit:
        raise AnimationError('Animation size outside upload budget')
    with Image.open(path) as image:
        if (image.format not in {'WEBP', 'GIF'} or not getattr(image, 'is_animated', False)
                or not 2 <= image.n_frames <= MAX_FRAMES or image.info.get('loop') != 0
                or image.size != (expected_width, expected_height) or max(image.size) > MAX_SIDE):
            raise AnimationError(f'Invalid animation format={image.format} frames={image.n_frames} '
                                 f'loop={image.info.get("loop")} size={image.size}', reason='invalid_animation')
        duration = 0
        first = None
        changed = False
        for index in range(image.n_frames):
            image.seek(index)
            image.load()
            digest = hashlib.blake2b(image.convert('RGBA').tobytes(), digest_size=16).digest()
            if first is None:
                first = digest
            changed |= digest != first
            delay = image.info.get('duration', 0)
            if not isinstance(delay, (int, float)) or delay < 1000 / 60 - 1:
                raise AnimationError(f'Invalid/high-FPS frame duration={delay}', reason='invalid_timing')
            duration += delay / 1000
        # Keep the previous two-frame rounding tolerance for ordinary GIFs.
        # Tiny animations need a tighter check: 80 -> 120 ms would change their speed.
        tolerance = max(0.01, expected_duration * 0.05) if expected_duration < 0.25 else 2 / 12
        if not changed or abs(duration - expected_duration) > tolerance:
            raise AnimationError(f'Animation timing changed={changed} duration={duration:.6f} '
                                 f'expected={expected_duration:.6f} tolerance={tolerance:.6f}', reason='invalid_timing')
        return {'format': image.format, 'frames': image.n_frames, 'loop': 0,
                'duration': duration, 'width': image.width, 'height': image.height,
                'bytes': path.stat().st_size}


@dataclass(frozen=True)
class EncodingProfile:
    name: str
    gif: bool
    side: int
    fps: int
    quality: int
    colors: int = 128


PROFILES = (
    EncodingProfile('webp_480_12_q70', False, 480, 12, 70),
    EncodingProfile('webp_320_10_q65', False, 320, 10, 65),
)
GIF_PROFILE = EncodingProfile('gif_320_10_c96', True, 320, 10, 0, 96)


def conversion_command(ffmpeg: str, source: Path, info: SourceInfo, *, gif: bool,
                       profile: EncodingProfile | None = None) -> tuple[list[str], int, int]:
    profile = profile or (GIF_PROFILE if gif else PROFILES[0])
    width, height = target_size(info, profile.side)
    # A fixed 12/10 FPS filter can drop one of two frames in an 80 ms animation.
    filters = ('' if info.preserve_short_frames else f'fps={profile.fps},')
    filters += f'scale={width}:{height}:flags=bilinear,setsar=1'
    command = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-max_alloc', '67108864',
               '-threads', '1', '-filter_threads', '1', '-filter_complex_threads', '1',
               '-protocol_whitelist', 'file', '-f', 'mov', '-enable_drefs', '0', '-use_absolute_path', '0',
               '-i', str(source.resolve()), '-map', '[out]' if gif else '0:v:0', '-an', '-sn', '-dn', '-map_metadata', '-1',
               '-t', str(MAX_SECONDS + 1), '-frames:v', str(MAX_FRAMES), '-threads', '1']
    if info.preserve_short_frames:
        command += ['-enc_time_base', '1:1000']
    if gif:
        # Per-frame palettes keep this graph streaming; a whole-clip palette buffers frames.
        filters += (f',split[a][b];[a]palettegen=max_colors={profile.colors}:stats_mode=single[p];'
                    '[b][p]paletteuse=new=1:dither=bayer:bayer_scale=3[out]')
        command += ['-filter_complex', filters, '-c:v', 'gif', '-gifflags', '+transdiff', '-loop', '0', '-f', 'gif', 'pipe:1']
    else:
        command += ['-vf', filters, '-c:v', 'libwebp_anim', '-quality', str(profile.quality), '-compression_level', '0',
                    '-lossless', '0', '-loop', '0', '-f', 'webp', 'pipe:1']
    return command, width, height


def correct_short_webp_duration(path: Path, expected_duration: float):
    """Correct libwebp's padded final-frame hold without re-encoding/removing any frame.

    ANMF stores a 24-bit duration at byte 12 of its frame header. In passthrough
    mode FFmpeg can repeat the preceding delay for the final frame (60+60 ms
    instead of a source's 60+20 ms). Only adjust this last hold in short GIFs.
    """
    with path.open('r+b') as stream:
        total_size = path.stat().st_size
        header = stream.read(12)
        if len(header) != 12 or header[:4] != b'RIFF' or header[8:] != b'WEBP':
            raise AnimationError('Invalid WebP header', reason='invalid_animation')
        if int.from_bytes(header[4:8], 'little') + 8 != total_size:
            raise AnimationError('Incomplete WebP container', reason='invalid_animation')
        frames = []
        position = 12
        while position < total_size:
            stream.seek(position)
            chunk = stream.read(8)
            if len(chunk) != 8:
                raise AnimationError('Truncated WebP chunk', reason='invalid_animation')
            kind, size = struct.unpack('<4sI', chunk)
            end = position + 8 + size + size % 2
            if end > total_size:
                raise AnimationError('Invalid WebP chunk size', reason='invalid_animation')
            if kind == b'ANMF':
                if size < 16 or len(frames) >= MAX_FRAMES:
                    raise AnimationError('Invalid WebP frames', reason='invalid_animation')
                stream.seek(position + 8 + 12)
                frames.append((stream.tell(), int.from_bytes(stream.read(3), 'little')))
            position = end
        if len(frames) < 2:
            raise AnimationError('WebP has fewer than two frames', reason='invalid_animation')
        expected_ms = round(expected_duration * 1000)
        remaining = expected_ms - sum(delay for _, delay in frames[:-1])
        if not 16 <= remaining <= 250 or abs(sum(delay for _, delay in frames) - expected_ms) > 100:
            raise AnimationError('Cannot preserve source frame timing', reason='invalid_timing')
        stream.seek(frames[-1][0])
        stream.write(remaining.to_bytes(3, 'little'))


async def convert_animation(source: Path, folder: Path, limit: int) -> tuple[Path, dict]:
    if under_pressure(reserve=RSS_LIMIT + limit):
        raise AnimationError('Not enough memory headroom for GIF', reason='memory_pressure')
    if worker_slot.locked():
        raise AnimationError('Another media worker is active', reason='busy')
    async with worker_slot:
        return await _convert_animation(source, folder, limit)


async def _convert_animation(source: Path, folder: Path, limit: int) -> tuple[Path, dict]:
    ffmpeg, ffprobe = binary('ffmpeg'), binary('ffprobe')
    metadata = folder / 'animation_probe.json'
    report = folder / 'animation_validation.json'
    started = time.monotonic()
    spent_cpu = 0.0

    async def invoke(command, destination, cap, *, timeout=35):
        nonlocal spent_cpu
        wall_left = TOTAL_WALL_SECONDS - (time.monotonic() - started)
        cpu_left = TOTAL_CPU_SECONDS - spent_cpu
        if wall_left <= 0 or cpu_left <= 0:
            raise AnimationError('Total GIF conversion budget exhausted', reason='total_budget')
        try:
            stats = await run_tool(command, destination, cap, timeout=min(timeout, wall_left),
                                   cpu_limit=min(CPU_SECONDS, cpu_left))
        except AnimationError as exc:
            spent_cpu += exc.details.get('cpu_seconds', 0)
            raise
        spent_cpu += (stats or {}).get('cpu_seconds', 0)
        return stats or {}

    await invoke([ffprobe, '-v', 'error', '-protocol_whitelist', 'file', '-f', 'mov',
                    '-enable_drefs', '0', '-select_streams', 'v:0', '-show_entries',
                    'stream=codec_name,profile,pix_fmt,width,height,sample_aspect_ratio,duration,avg_frame_rate,r_frame_rate,nb_frames:'
                    'stream_side_data=rotation:format=duration,size',
                    '-of', 'json', str(source.resolve())], metadata, 65536, timeout=10)
    data = json.loads(metadata.read_text(encoding='utf-8'))
    info = source_info(data)
    log.info('gif_source bytes=%d codec=%r width=%d height=%d duration=%s frames=%d '
             'fps=%s preserve_short_frames=%s',
             source.stat().st_size, data['streams'][0].get('codec_name'), info.width, info.height, info.duration, info.frame_count,
             info.frame_rate, info.preserve_short_frames)
    failures = []
    profile = PROFILES[0]
    for attempt in range(2):
        gif = profile.gif
        destination = folder / ('animation.gif' if gif else 'animation.webp')
        command, width, height = conversion_command(ffmpeg, source, info, gif=gif, profile=profile)
        stage = 'encode'
        log.info('gif_attempt profile=%s stage=%s width=%d height=%d output_limit=%d',
                 profile.name, stage, width, height, limit)
        try:
            await invoke(command, destination, limit)
            if info.preserve_short_frames and not gif:
                correct_short_webp_duration(destination, info.duration)
            stage = 'validate'
            # Validate in a bounded process as well, keeping frame decoding off the bot event loop.
            await invoke([sys.executable, '-S', str(Path(__file__).resolve()), '--inspect', str(destination.resolve()),
                            str(info.duration), str(width), str(height), str(limit)], report, 65536, timeout=10)
            result = json.loads(report.read_text(encoding='utf-8'))
            result['profile'] = profile.name
            result['preserve_short_frames'] = info.preserve_short_frames
            log.info('gif_attempt profile=%s result=success format=%s frames=%s bytes=%s duration=%s',
                     profile.name, result['format'], result['frames'], result['bytes'], result['duration'])
            return destination, result
        except (AnimationError, OSError, ValueError, TimeoutError) as exc:
            destination.unlink(missing_ok=True)
            reason = getattr(exc, 'reason', type(exc).__name__)
            failures.append({'profile': profile.name, 'stage': stage, 'reason': reason,
                             'tool': getattr(exc, 'details', {})})
            log.info('gif_attempt profile=%s stage=%s result=failed reason=%s detail=%s',
                     profile.name, stage, reason, str(exc))
            if attempt == 1 or reason in {'total_budget', 'busy'}:
                break
            # Only two encodes in total: size/resource pressure -> smaller WebP;
            # unavailable/broken WebP -> one real palette GIF as the second attempt.
            profile = (PROFILES[1] if reason in {'output_limit', 'memory_limit', 'cpu_limit', 'timeout'}
                       else GIF_PROFILE)
    raise AnimationError('Could not produce a full looping animation within resource/upload limits',
                         reason='attempts_exhausted', details={'attempts': failures})


if __name__ == '__main__':
    try:
        _, path, duration, width, height, limit = sys.argv[1:]
        result = inspect_animation(Path(path), float(duration), int(width), int(height), int(limit))
        print(json.dumps(result))
    except Exception as exc:
        print(f'animation_validation_failed type={type(exc).__name__} '
              f'reason={getattr(exc, "reason", "decode_failed")} detail={exc}', file=sys.stderr)
        raise SystemExit(1)
