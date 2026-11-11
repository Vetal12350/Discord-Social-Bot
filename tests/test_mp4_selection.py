"""Regression: yt-dlp must not silently skip oversized TikTok MP4s."""
import tempfile
import unittest
from pathlib import Path

from media import safe_worker_failure
from media_worker import (
    NoMP4OutputError, ensure_mp4_output, safe_failure_reason,
    select_bounded_mp4,
)

LIMIT = 19 * 1024 * 1024


def fmt(quality, size=None, approx=None, *,
        video='avc1.640028', audio='mp4a.40.2',
        ext='mp4', protocol='https'):
    item = {
        'ext': ext, 'protocol': protocol, 'vcodec': video,
        'acodec': audio, 'height': quality, 'width': round(quality * 9 / 16),
        'tbr': quality * 2,
    }
    if size is not None:
        item['filesize'] = size
    if approx is not None:
        item['filesize_approx'] = approx
    return item


class BoundedMP4Tests(unittest.TestCase):
    def test_prefers_highest_quality_that_fits(self):
        small = fmt(360, 4 * 1024 * 1024)
        good = fmt(720, 12 * 1024 * 1024)
        huge = fmt(1080, 38 * 1024 * 1024)
        self.assertEqual(
            list(select_bounded_mp4({'formats': [small, huge, good]}, LIMIT)),
            [good])

    def test_real_tiktok_codec_labels_h264_aac_are_accepted(self):
        # Live VPS output: format=mp4, protocol=https, video=h264,
        # audio=aac. This used to yield FITTING_MP4: 0 despite 3
        # progressive formats below the 19 MiB upload limit.
        candidates = [
            fmt(1024, size=int(10.77 * 1048576), video='h264', audio='aac'),
            fmt(1024, size=int(12.04 * 1048576), video='h264', audio='aac'),
            fmt(1024, size=int(15.36 * 1048576), video='h264', audio='aac'),
            fmt(1024, size=int(26.98 * 1048576), video='h264', audio='aac'),
            fmt(1024, size=int(10.19 * 1048576), video='h265', audio='none'),
        ]
        selected = list(select_bounded_mp4({'formats': candidates}, LIMIT))
        self.assertEqual(len(selected), 1)
        self.assertIn(selected[0], candidates[:3])
        self.assertEqual(selected[0]['acodec'], 'aac')
        self.assertEqual(selected[0]['vcodec'], 'h264')
        self.assertLess(selected[0]['filesize'], LIMIT)

    def test_raw_aac_preferred_to_no_audio(self):
        mute = fmt(720, size=7 * 1048576, video='h264', audio='none')
        sound = fmt(720, size=9 * 1048576, video='h264', audio='aac')
        selected = list(select_bounded_mp4({'formats': [mute, sound]}, LIMIT))
        self.assertEqual(selected, [sound])

    def test_approx_size_uses_10_percent_safety_margin(self):
        safe = fmt(480, approx=15 * 1024 * 1024)
        risky = fmt(720, approx=18 * 1024 * 1024)
        self.assertEqual(
            list(select_bounded_mp4({'formats': [risky, safe]}, LIMIT)),
            [safe])

    def test_only_oversized_mp4_fails_closed(self):
        self.assertEqual(
            list(select_bounded_mp4({'formats': [fmt(1080, 25 * 1024 * 1024)]}, LIMIT)),
            [])

    def test_unknown_size_prefers_moderate_quality(self):
        choices = [fmt(1080), fmt(360), fmt(720), fmt(480)]
        self.assertEqual(
            list(select_bounded_mp4({'formats': choices}, LIMIT)), [choices[2]])

    def test_reject_incompatible_and_fragment_formats(self):
        choices = [
            fmt(720, size=5, video='vp9'),
            fmt(720, size=5, audio='opus'),
            fmt(720, size=5, ext='webm'),
            fmt(720, size=5, protocol='m3u8_native'),
            fmt(720, size=5, protocol='http'),
        ]
        self.assertEqual(list(select_bounded_mp4({'formats': choices}, LIMIT)), [])

    def test_audio_present_preferred_if_quality_equal(self):
        mute = fmt(720, size=8 * 1024 * 1024, audio='none')
        sound = fmt(720, size=9 * 1024 * 1024)
        self.assertEqual(list(select_bounded_mp4({'formats': [mute, sound]}, LIMIT)), [sound])

    def test_skip_without_mp4_is_failure_not_success(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(NoMP4OutputError):
                ensure_mp4_output(folder)
            output = Path(folder) / 'video.mp4'
            output.write_bytes(b'')
            with self.assertRaises(NoMP4OutputError):
                ensure_mp4_output(folder)
            output.write_bytes(b'nonempty')
            ensure_mp4_output(folder)  # Codec/MP4 validation remains at parent.

    def test_error_category_contains_no_provider_urls(self):
        self.assertEqual(safe_failure_reason(NoMP4OutputError('secret_url')),
                         'no_mp4_output')
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / 'worker.log'
            log.write_text('media_worker_reason=no_mp4_output\n', encoding='utf-8')
            self.assertEqual(safe_worker_failure(log), 'no_mp4_output')


if __name__ == '__main__':
    unittest.main()
