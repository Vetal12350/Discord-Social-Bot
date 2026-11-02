"""Safe media-worker diagnostic categories; never leak signed URLs."""
import tempfile
import unittest
from pathlib import Path

from media import safe_worker_failure
from media_worker import safe_failure_reason


class WorkerFailureTests(unittest.TestCase):
    def test_known_extractor_failure_categories(self):
        DownloadError = type('DownloadError', (Exception,), {})
        examples = {
            'Requested format is not available': 'format_unavailable',
            'HTTP Error 403: Forbidden': 'http_403',
            'HTTP Error 429: Too Many Requests': 'http_429',
            'TikTok requires login': 'access_challenge',
            'Generic TikTok extraction failure': 'extractor_failed',
        }
        for problem, expected in examples.items():
            with self.subTest(problem=expected):
                self.assertEqual(safe_failure_reason(DownloadError(problem)), expected)

    def test_unknown_error_not_logged(self):
        self.assertEqual(safe_failure_reason(RuntimeError('secret_signed_url=abc')), 'worker_error')

    def test_worker_log_read_does_not_return_unsafe_text(self):
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp) / 'worker.log'
            file.write_text('ERROR: token=secret signed_url=abc\n'
                            'media_worker_reason=http_403\n', encoding='utf-8')
            self.assertEqual(safe_worker_failure(file), 'http_403')
            file.write_text('media_worker_reason=http_403&secret=abc\n', encoding='utf-8')
            self.assertEqual(safe_worker_failure(file), 'unclassified')

    def test_worker_log_missing(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertEqual(safe_worker_failure(Path(temp) / 'none'), 'unclassified')


if __name__ == '__main__':
    unittest.main()
