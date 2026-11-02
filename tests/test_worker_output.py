"""Regression: TikTok MP4 download succeeds even if worker stdout is not JSON."""
import tempfile
import unittest
from pathlib import Path

from media import parse_worker_output


class WorkerOutputTests(unittest.TestCase):
    def test_download_ignores_empty_stdout(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'result.json'
            output.write_text('', encoding='utf-8')
            self.assertEqual(parse_worker_output(output, download=True), {})

    def test_download_ignores_unparseable_stdout(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'result.json'
            output.write_text('provider progress text, not JSON', encoding='utf-8')
            self.assertEqual(parse_worker_output(output, download=True), {})

    def test_metadata_valid_json_is_required(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'result.json'
            output.write_text('{"description":"sample","id":"123"}', encoding='utf-8')
            self.assertEqual(parse_worker_output(output, download=False)['id'], '123')

    def test_metadata_empty_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'result.json'
            output.write_text('', encoding='utf-8')
            with self.assertRaises(RuntimeError):
                parse_worker_output(output, download=False)

    def test_metadata_malformed_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'result.json'
            output.write_text('garbage', encoding='utf-8')
            with self.assertRaises(RuntimeError):
                parse_worker_output(output, download=False)

    def test_metadata_list_not_accepted(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'result.json'
            output.write_text('[1, 2]', encoding='utf-8')
            with self.assertRaises(RuntimeError):
                parse_worker_output(output, download=False)


if __name__ == '__main__':
    unittest.main()
