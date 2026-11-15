"""TikTok MP4: try other CDN copies of the *same* format on HTTP 403/404 only."""
import contextlib
import io
from pathlib import Path
import tempfile
import unittest

from yt_dlp.utils import DownloadError

from media import safe_worker_mirror
from media_worker import MAX_VIDEO_MIRRORS, MirrorPlan, download_with_mirrors, same_format_mirrors

LIMIT = 19 * 1024 * 1024


def fmt(host, *, height=1024, size=700_000):
    return {'ext': 'mp4', 'protocol': 'https', 'vcodec': 'h264', 'acodec': 'aac',
            'width': 576, 'height': height, 'filesize': size,
            'url': f'https://{host}.tiktokcdn.com/video/abc.mp4'}


FORMATS = [fmt('v16'), fmt('v19'), fmt('v16', height=720, size=400_000), fmt('v77'), fmt('v99')]


class FakeDownloader:
    """Emulates yt-dlp re-processing: the selector picks; a dead URL raises."""

    def __init__(self, plan, folder, failures):
        self.plan, self.folder, self.failures, self.urls = plan, Path(folder), failures, []

    def process_ie_result(self, info, download):
        chosen = next(self.plan(info), None)
        if chosen is None:
            raise DownloadError('ERROR: Requested format is not available')
        self.urls.append(chosen['url'])
        (self.folder / 'video.mp4.part').write_bytes(b'partial')
        failure = self.failures.get(chosen['url'].split('//')[1].split('.')[0])
        if failure:
            raise DownloadError(failure)
        (self.folder / 'video.mp4.part').unlink()
        (self.folder / 'video.mp4').write_bytes(b'mp4')
        return {**info, 'url': chosen['url']}


class MirrorSelectionTests(unittest.TestCase):
    def test_only_identical_formats_on_other_urls(self):
        primary = FORMATS[0]
        mirrors = same_format_mirrors(FORMATS + [dict(FORMATS[1])], primary)
        self.assertEqual([m['url'].split('//')[1][:3] for m in mirrors], ['v19', 'v77', 'v99'])

    def test_plan_is_bounded_and_keeps_size_aware_primary(self):
        plan = MirrorPlan(LIMIT)
        picked = []
        for attempt in range(5):
            plan.attempt = attempt
            picked.append(next(plan({'formats': FORMATS}), None))
        self.assertEqual(plan.available, MAX_VIDEO_MIRRORS)
        self.assertEqual([p['url'][8:11] for p in picked[:3]], ['v16', 'v19', 'v77'])
        self.assertEqual(picked[3:], [None, None])
        self.assertTrue(all(p['height'] == 1024 for p in picked[:3]))


class MirrorDownloadTests(unittest.TestCase):
    def run_download(self, failures):
        with tempfile.TemporaryDirectory() as folder:
            plan = MirrorPlan(LIMIT)
            fake = FakeDownloader(plan, folder, failures)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                try:
                    result = download_with_mirrors(fake, {'formats': FORMATS}, plan, folder)
                except DownloadError as exc:
                    result = exc
            files = sorted(p.name for p in Path(folder).iterdir())
        return fake.urls, result, stderr.getvalue(), files

    def test_forbidden_copy_falls_back_to_same_format_mirror(self):
        urls, result, stderr, files = self.run_download(
            {'v16': 'ERROR: unable to download video data: HTTP Error 403: Forbidden'})
        self.assertEqual([u[8:11] for u in urls], ['v16', 'v19'])
        self.assertEqual(result['url'][8:11], 'v19')
        self.assertEqual(stderr, 'media_worker_mirror candidate=2 of=3\n')
        self.assertEqual(files, ['video.mp4'])  # no stale .part from the dead copy

    def test_all_copies_missing_fails_after_bounded_attempts(self):
        dead = 'ERROR: unable to download video data: HTTP Error 404: Not Found'
        urls, result, stderr, _ = self.run_download({'v16': dead, 'v19': dead, 'v77': dead, 'v99': dead})
        self.assertEqual(len(urls), MAX_VIDEO_MIRRORS)
        self.assertIsInstance(result, DownloadError)
        self.assertEqual(stderr, '')

    def test_other_failures_never_try_mirrors(self):
        for message in ('ERROR: unable to download video data: HTTP Error 500: Server Error',
                        'ERROR: unable to download video data: HTTP Error 429: Too Many Requests',
                        'ERROR: File is larger than max-filesize',
                        'ERROR: Requested format is not available'):
            with self.subTest(message=message):
                urls, result, _, _ = self.run_download({'v16': message})
                self.assertEqual(len(urls), 1)
                self.assertIsInstance(result, DownloadError)

    def test_success_on_first_copy_emits_no_marker(self):
        urls, result, stderr, _ = self.run_download({})
        self.assertEqual(len(urls), 1)
        self.assertEqual(stderr, '')


class MirrorMarkerTests(unittest.TestCase):
    def test_parent_accepts_only_fixed_marker(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'stderr'
            for text, expected in (('media_worker_mirror candidate=2 of=3\n', (2, 3)),
                                   ('media_worker_mirror candidate=3 of=2\n', None),
                                   ('media_worker_mirror candidate=2 of=3 https://x.tiktokcdn.com/a\n', None),
                                   ('media_worker_mirror candidate=9 of=9\n', None),
                                   ('junk\n', None)):
                with self.subTest(text=text):
                    path.write_text(text)
                    self.assertEqual(safe_worker_mirror(path), expected)


if __name__ == '__main__':
    unittest.main()
