"""Fixed HTTP diagnostics reach parent logs even when no retry can run."""
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from yt_dlp.networking import Request
from yt_dlp.networking.exceptions import HTTPError

from media import MAX_BYTES, _run_media, safe_worker_http_failures
from test_parent_worker_guards import Process
from test_tiktok_requests import CDN, POST, error
from tiktok_requests import BoundedTikTokGet, MAX_HTTP_DIAGNOSTICS, http_category, http_phase


class HttpDiagnosticTests(unittest.TestCase):
    def test_denied_retry_preserves_phase_status_and_fixed_reason(self):
        cases = [
            (Request('https://vt.tiktok.com/short', method='HEAD'), error(403), 'short_redirect', 'first_party_403'),
            (Request('https://vm.tiktok.com/short'), error(404), 'short_redirect', 'status_not_retryable'),
            (Request(POST), error(403), 'metadata_get', 'first_party_403'),
            (Request(CDN), error(403, {'Content-Type': 'text/html'}), 'media_get', 'html_challenge'),
            (Request(CDN), error(404), 'media_get', 'status_not_retryable'),
            (Request(CDN, headers={'Authorization': 'AUTH_SECRET'}), error(503), 'media_get', 'authorization'),
            (Request(CDN, headers={'proxy-authorization': 'AUTH_SECRET'}), error(503), 'media_get', 'authorization'),
            (Request(CDN, method='POST', data=b'BODY_SECRET'), error(503), 'media_get', 'method_not_safe'),
            (Request(CDN, method='GET', data=b'BODY_SECRET'), error(503), 'media_get', 'request_body'),
            (Request('https://evil.example/private'), error(503), 'other', 'url_policy'),
            (Request(CDN), error(429, {'Retry-After': '60'}), 'media_get', 'retry_after_not_eligible'),
        ]
        for request, exc, phase, reason in cases:
            send = Mock(side_effect=exc)
            with self.subTest(reason=reason), patch('tiktok_requests.sys.stderr', new_callable=io.StringIO) as output, \
                 patch('tiktok_requests.time.sleep') as sleep:
                with self.assertRaises(type(exc)) as raised:
                    BoundedTikTokGet().open(send, request)
            self.assertIs(raised.exception, exc)
            send.assert_called_once()
            sleep.assert_not_called()
            self.assertEqual(output.getvalue(), f'media_worker_http_failure phase={phase} '
                             f'status={http_category(exc.status)} retry=skipped retry_reason={reason}\n')
            for secret in ('DO_NOT_LOG', 'AUTH_SECRET', 'BODY_SECRET', 'https://', 'private'):
                self.assertNotIn(secret, output.getvalue())

    def test_redirect_loop_is_named_without_retry(self):
        exc = error(503)
        exc.redirect_loop = True
        with patch('tiktok_requests.sys.stderr', new_callable=io.StringIO) as output, \
             patch('tiktok_requests.time.sleep') as sleep, self.assertRaises(type(exc)):
            BoundedTikTokGet().open(Mock(side_effect=exc), Request(CDN))
        self.assertIn('retry_reason=redirect_loop', output.getvalue())
        sleep.assert_not_called()

    def test_second_http_failure_and_later_request_show_exhausted_budget(self):
        retry = BoundedTikTokGet()
        send = Mock(side_effect=[error(503), error(404), error(429)])
        with patch('tiktok_requests.sys.stderr', new_callable=io.StringIO) as output, \
             patch('tiktok_requests.time.sleep') as sleep:
            for _ in range(2):
                with self.assertRaises(HTTPError):
                    retry.open(send, Request(CDN))
        lines = [line for line in output.getvalue().splitlines() if line.startswith('media_worker_http_failure ')]
        self.assertEqual(len(lines), 3)
        self.assertIn('status=http_503 retry=scheduled retry_reason=eligible', lines[0])
        self.assertIn('status=http_404 retry=skipped retry_reason=retry_budget_exhausted', lines[1])
        self.assertIn('status=http_429 retry=skipped retry_reason=retry_budget_exhausted', lines[2])
        self.assertEqual(send.call_count, 3)
        sleep.assert_called_once()

    def test_categories_and_request_phases_do_not_echo_arbitrary_values(self):
        for url, phase in ((CDN, 'media_get'), ('https://p16.tiktokcdn-eu.com/a', 'media_get'),
                           ('https://www.tiktok.com/t/short/', 'short_redirect'),
                           (POST, 'metadata_get'), ('https://x.com/i/status/123', 'other'),
                           ('https://tiktok.com.evil.example/a', 'other'), ('https://[bad', 'unknown'), ('', 'unknown')):
            self.assertEqual(http_phase(url), phase)
        for status, category in ((403, 'http_403'), (451, 'http_4xx'), (599, 'http_5xx'),
                                 (302, 'http_other'), ('SECRET', 'http_unknown')):
            self.assertEqual(http_category(status), category)

    def test_child_and_parent_diagnostics_are_bounded_and_reject_untrusted_text(self):
        retry = BoundedTikTokGet()
        with patch('tiktok_requests.sys.stderr', new_callable=io.StringIO) as output:
            for _ in range(20):
                retry.record_failure(CDN, 404, 'status_not_retryable')
        self.assertEqual(len(output.getvalue().splitlines()), MAX_HTTP_DIAGNOSTICS)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'worker.log'
            marker = output.getvalue().splitlines()[0] + '\n'
            path.write_text('URL signature=SECRET\n' + marker * 20)
            self.assertEqual(safe_worker_http_failures(path),
                             [('media_get', 'http_404', 'skipped', 'status_not_retryable')] * MAX_HTTP_DIAGNOSTICS)
            for invalid in (marker.rstrip() + ' secret=SECRET\n',
                            marker.replace('phase=media_get', 'phase=SECRET'),
                            marker.replace('status=http_404', 'status=SECRET'),
                            marker.replace('retry=skipped', 'retry=scheduled'),
                            marker.replace('retry_reason=status_not_retryable', 'retry_reason=SECRET'),
                            'x' * 65536 + '\n' + marker):
                path.write_text(invalid)
                self.assertEqual(safe_worker_http_failures(path), [])


class ParentHttpDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_retry_diagnostic_reaches_parent_on_worker_failure_and_success(self):
        for returncode in (0, 1):
            process = Process(returncode=returncode)
            close = Mock()

            async def spawn(*args, **kwargs):
                kwargs['stderr'].write(b'unsafe signature=SECRET\nmedia_worker_http_failure '
                    b'phase=metadata_get status=http_403 retry=skipped retry_reason=first_party_403\n'
                    b'media_worker_reason=http_403\n')
                kwargs['stderr'].flush()
                return process

            with self.subTest(returncode=returncode), tempfile.TemporaryDirectory() as directory, \
                 patch('media.asyncio.create_subprocess_exec', side_effect=spawn) as start, \
                 patch('media.limit_process', return_value=(None, close)), self.assertLogs('media', 'INFO') as captured:
                if returncode:
                    with self.assertRaises(RuntimeError):
                        await _run_media('https://vt.tiktok.com/ZSpQHULbU?secret=SECRET',
                                         Path(directory), download=True, limit=MAX_BYTES)
                else:
                    await _run_media('https://vt.tiktok.com/ZSpQHULbU?secret=SECRET',
                                     Path(directory), download=True, limit=MAX_BYTES)
            output = '\n'.join(captured.output)
            self.assertIn('source_key=tiktok-short:ZSpQHULbU mode=download phase=metadata_get '
                          'status=http_403 retry=skipped retry_reason=first_party_403', output)
            self.assertNotIn('SECRET', output)
            start.assert_awaited_once()
            close.assert_called_once()
