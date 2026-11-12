"""Parent-side RuntimeError diagnostics without weakening limits or cleanup."""
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from link_utils import source_log_key
from media import MAX_BYTES, _run_media, run_media
from process_limits import RSS_LIMIT

SOURCE = 'https://vt.tiktok.com/ZSp98gAmN/?secret=do-not-log'


class Process:
    pid = 42

    def __init__(self, returncode=None):
        self.returncode = returncode
        self.killed = False

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        return self.returncode


class ParentGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_memory_pressure_is_named_before_starting_child(self):
        with tempfile.TemporaryDirectory() as temp, \
             patch('media.under_pressure', return_value=True), \
             patch('media.asyncio.create_subprocess_exec', new_callable=AsyncMock) as spawn, \
             self.assertLogs('media', level='INFO') as captured:
            with self.assertRaisesRegex(RuntimeError, 'skipped under memory pressure'):
                await run_media(SOURCE, Path(temp), download=True)
        spawn.assert_not_awaited()
        self.assertIn('reason=memory_pressure', captured.output[0])
        self.assertIn('source_key=tiktok-short:ZSp98gAmN', captured.output[0])
        self.assertNotIn('do-not-log', captured.output[0])

    async def check_guard(self, reason):
        process = Process()
        close = Mock()
        stats = SimpleNamespace(
            memory_info=lambda: SimpleNamespace(rss=RSS_LIMIT + 1 if reason == 'rss_limit' else 1234),
            cpu_times=lambda: SimpleNamespace(user=8.1 if reason == 'cpu_limit' else 0.1, system=0.1))

        async def spawn(*args, **kwargs):
            if reason == 'directory_limit':
                with (folder / 'video.mp4').open('wb') as output:
                    output.truncate(MAX_BYTES + 512 * 1024 + 1)
            elif reason == 'stdout_limit':
                kwargs['stdout'].truncate(1024 * 1024 + 1)
            elif reason == 'stderr_limit':
                kwargs['stderr'].truncate(65537)
            # The error log may contain provider secrets, which must not be read.
            kwargs['stderr'].write(b'https://video.tiktok.com/file?signature=DO_NOT_LOG\n')
            kwargs['stderr'].flush()
            return process

        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            with patch('media.asyncio.create_subprocess_exec', side_effect=spawn), \
                 patch('media.limit_process', return_value=(stats, close)), \
                 self.assertLogs('media', level='INFO') as captured:
                with self.assertRaises(RuntimeError):
                    await _run_media(SOURCE, folder, download=True, limit=MAX_BYTES)
        output = '\n'.join(captured.output)
        self.assertIn('reason=' + reason, output)
        self.assertIn('elapsed_ms=', output)
        self.assertIn('directory_bytes=', output)
        self.assertIn('rss_limit=' + str(RSS_LIMIT), output)
        self.assertIn('cpu_limit=8', output)
        self.assertNotIn('DO_NOT_LOG', output)
        self.assertNotIn('signature', output)
        self.assertNotIn('do-not-log', output)
        self.assertTrue(process.killed)
        close.assert_called_once()

    async def test_each_parent_budget_has_a_distinct_reason_and_kills_child(self):
        for reason in ('rss_limit', 'cpu_limit', 'directory_limit', 'stdout_limit', 'stderr_limit'):
            with self.subTest(reason=reason):
                await self.check_guard(reason)

    async def test_wall_timeout_is_logged_and_cleanup_still_runs(self):
        process = Process()
        close = Mock()
        class Timeout:
            async def __aenter__(self):
                raise TimeoutError()

            async def __aexit__(self, *_):
                return False
        with tempfile.TemporaryDirectory() as temp, \
             patch('media.asyncio.create_subprocess_exec', new_callable=AsyncMock, return_value=process), \
             patch('media.limit_process', return_value=(object(), close)), \
             patch('media.asyncio.timeout', return_value=Timeout()), \
             self.assertLogs('media', level='INFO') as captured:
            with self.assertRaises(TimeoutError):
                await _run_media(SOURCE, Path(temp), download=True, limit=MAX_BYTES)
        self.assertIn('reason=wall_timeout', captured.output[0])
        self.assertTrue(process.killed)
        close.assert_called_once()

    async def test_http_failure_has_identity_and_exit_code_without_retry(self):
        process = Process(returncode=1)
        async def spawn(*args, **kwargs):
            kwargs['stderr'].write(b'unsafe signed URL here\nmedia_worker_reason=http_403\n')
            kwargs['stderr'].flush()
            return process
        with tempfile.TemporaryDirectory() as temp, \
             patch('media.asyncio.create_subprocess_exec', side_effect=spawn) as start, \
             patch('media.limit_process', return_value=(None, None)), \
             self.assertLogs('media', level='INFO') as captured:
            with self.assertRaisesRegex(RuntimeError, 'Public media unavailable'):
                await _run_media(SOURCE, Path(temp), download=True, limit=MAX_BYTES)
        start.assert_awaited_once()
        self.assertIn('reason=http_403', captured.output[0])
        self.assertIn('source_key=tiktok-short:ZSp98gAmN returncode=1', captured.output[0])
        self.assertNotIn('unsafe signed URL', captured.output[0])

    async def test_completed_child_metadata_still_logs_stdout_limit(self):
        process = Process(returncode=0)
        async def spawn(*args, **kwargs):
            kwargs['stdout'].truncate(1024 * 1024 + 1)
            return process
        with tempfile.TemporaryDirectory() as temp, \
             patch('media.asyncio.create_subprocess_exec', side_effect=spawn), \
             patch('media.limit_process', return_value=(None, None)), \
             self.assertLogs('media', level='INFO') as captured:
            with self.assertRaisesRegex(RuntimeError, 'Metadata too large'):
                await _run_media(SOURCE, Path(temp), download=False, limit=MAX_BYTES)
        self.assertIn('reason=stdout_limit', captured.output[0])
        self.assertIn('mode=metadata', captured.output[0])
        self.assertFalse(process.killed)


class LogIdentityTests(unittest.TestCase):
    def test_numeric_ids_and_shortcodes_without_handles_or_query(self):
        self.assertEqual(source_log_key(SOURCE), 'tiktok-short:ZSp98gAmN')
        self.assertEqual(source_log_key('https://www.tiktok.com/@privatehandle/video/7706917669521427730?signature=secret'),
                         'tiktok:7706917669521427730')
        self.assertEqual(source_log_key('https://fxtwitter.com/handle/status/123/video/1?secret=value'), 'x:123')

    def test_cdn_or_malicious_input_is_never_echoed(self):
        for url in ('https://video.tiktokcdn.com/file?signature=secret',
                    'https://evil.example/secret', 'https://vt.tiktok.com/ZSp98gAmN/\nTOKEN'):
            self.assertEqual(source_log_key(url), 'unsupported')
