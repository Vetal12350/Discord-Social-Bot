"""Retry only pre-body public GET/HEAD within one existing worker budget."""
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from yt_dlp.networking import Request, Response
from yt_dlp.networking.exceptions import HTTPError, TransportError

from media import safe_worker_retry
from tiktok_requests import BoundedTikTokGet, retry_delay

CDN = 'https://v16-webapp-prime.tiktok.com/video/file?signature=DO_NOT_LOG'
POST = 'https://www.tiktok.com/@test/video/7707467025265757460'


def error(status, headers=None, url=CDN):
    return HTTPError(Response(io.BytesIO(b'error'), url, headers=headers or {}, status=status))


class RetryTests(unittest.TestCase):
    def test_one_retry_before_body_without_changing_request(self):
        for status in (403, 429, 500, 502, 503, 504):
            with self.subTest(status=status):
                request = Request(CDN)
                result = object()
                send = Mock(side_effect=[error(status), result])
                with patch('tiktok_requests.time.sleep') as sleep, patch('tiktok_requests.sys.stderr', new_callable=io.StringIO) as log:
                    self.assertIs(BoundedTikTokGet().open(send, request), result)
                self.assertEqual(send.call_count, 2)
                self.assertIs(send.call_args_list[0].args[0], request)
                self.assertIs(send.call_args_list[1].args[0], request)
                sleep.assert_called_once_with(1.0)
                self.assertNotIn('DO_NOT_LOG', log.getvalue())
                self.assertIn(f'reason=http_{status}', log.getvalue())

    def test_budget_is_shared_by_all_urls_of_worker(self):
        retry = BoundedTikTokGet()
        with patch('tiktok_requests.time.sleep'), patch('tiktok_requests.sys.stderr', new_callable=io.StringIO):
            retry.open(Mock(side_effect=[error(503), object()]), Request(POST))
            send = Mock(side_effect=error(503))
            with self.assertRaises(HTTPError):
                retry.open(send, Request(CDN))
        send.assert_called_once()

    def test_second_error_is_propagated_without_third_request(self):
        first, second = error(503), error(503)
        send = Mock(side_effect=[first, second])
        with patch('tiktok_requests.time.sleep'), patch('tiktok_requests.sys.stderr', new_callable=io.StringIO):
            with self.assertRaises(HTTPError) as raised:
                BoundedTikTokGet().open(send, Request(CDN))
        self.assertIs(raised.exception, second)
        self.assertEqual(send.call_count, 2)

    def test_first_party_403_html_auth_post_and_nonretryable_status_are_not_retried(self):
        cases = [(Request(POST), error(403, url=POST)),
                 (Request('https://vt.tiktok.com/ZSpQHULbU', method='HEAD'), error(403)),
                 (Request(CDN), error(403, {'Content-Type': 'text/html'})),
                 (Request(CDN), error(403, {'content-type': 'text/html; charset=utf-8'})),
                 (Request(CDN, method='POST', data=b'data'), error(503)),
                 (Request(CDN, headers={'Authorization': 'secret'}), error(503)),
                 (Request('https://attacker.example/file'), error(503)),
                 *[(Request(CDN), error(status)) for status in (400, 401, 404, 410, 451, 501)]]
        for request, exc in cases:
            with self.subTest(url_kind=request.method, status=exc.status):
                send = Mock(side_effect=exc)
                with patch('tiktok_requests.time.sleep') as sleep, self.assertRaises(HTTPError):
                    BoundedTikTokGet().open(send, request)
                send.assert_called_once()
                sleep.assert_not_called()

    def test_head_5xx_can_retry_and_error_response_is_closed(self):
        response = error(503)
        response.close = Mock()
        send = Mock(side_effect=[response, object()])
        with patch('tiktok_requests.time.sleep'), patch('tiktok_requests.sys.stderr', new_callable=io.StringIO):
            BoundedTikTokGet().open(send, Request(POST, method='HEAD'))
        response.close.assert_called_once()
        self.assertEqual(send.call_count, 2)

    def test_long_or_bad_retry_after_declines_retry(self):
        for value in ('3', '60', 'not-a-delay', 'Infinity'):
            send = Mock(side_effect=error(429, {'Retry-After': value}))
            with self.subTest(value=value), patch('tiktok_requests.time.sleep') as sleep:
                with self.assertRaises(HTTPError):
                    BoundedTikTokGet().open(send, Request(CDN))
                sleep.assert_not_called()
                send.assert_called_once()

    def test_short_retry_after_and_dates(self):
        self.assertEqual(retry_delay({'Retry-After': '2'}), 2)
        self.assertEqual(retry_delay({'Retry-After': '0'}), 0.25)
        self.assertEqual(retry_delay({'Retry-After': 'Thu, 01 Jan 1970 00:01:42 GMT'}, now=100), 2)
        self.assertIsNone(retry_delay({'Retry-After': 'Thu, 01 Jan 1970 00:02:00 GMT'}, now=100))

    def test_transport_policy_and_body_failures_are_not_retried(self):
        for exc in (TransportError('private network rejected'), TimeoutError(), OSError('DNS error')):
            send = Mock(side_effect=exc)
            with self.subTest(kind=type(exc).__name__), self.assertRaises(type(exc)):
                BoundedTikTokGet().open(send, Request(CDN))
            send.assert_called_once()

    def test_success_is_passed_through_without_reading_body(self):
        response = Mock()
        self.assertIs(BoundedTikTokGet().open(Mock(return_value=response), Request(CDN)), response)
        response.read.assert_not_called()

    def test_parent_reads_fixed_retry_marker_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'worker.log'
            path.write_text('unsafe URL signature=secret\nmedia_worker_retry reason=http_503 phase=media_get delay_ms=1000\n')
            self.assertEqual(safe_worker_retry(path), ('http_503', 'media_get', 1000))
            path.write_text('media_worker_retry reason=http_503 phase=media_get delay_ms=1000\n' + 'noise\n' * 32)
            self.assertEqual(safe_worker_retry(path), ('http_503', 'media_get', 1000))
            for value in ('media_worker_retry reason=http_403 phase=media_get delay_ms=9999',
                          'media_worker_retry reason=http_503 phase=media_get delay_ms=1000 secret=oops'):
                path.write_text(value)
                self.assertIsNone(safe_worker_retry(path))
