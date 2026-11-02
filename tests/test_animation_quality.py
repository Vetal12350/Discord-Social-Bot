"""Higher-quality X animated WebP profile and bounded fallback regression tests."""
import unittest

from animation import (
    GIF_PROFILE, MAX_FPS, MAX_FRAMES, MAX_SECONDS, MAX_SIDE,
    PROFILES, SourceInfo, conversion_command, target_size,
)


class AnimationQualityTests(unittest.TestCase):
    def test_primary_profile_is_sharper_and_smoother(self):
        primary = PROFILES[0]
        self.assertEqual((primary.side, primary.fps, primary.quality), (480, 15, 85))
        self.assertFalse(primary.gif)
        self.assertEqual(MAX_SIDE, 480)
        self.assertEqual(MAX_FPS, 15)
        self.assertEqual(MAX_FRAMES, MAX_SECONDS * MAX_FPS + 2)

    def test_fallback_stays_bounded_and_smaller(self):
        primary, fallback = PROFILES
        self.assertEqual((fallback.side, fallback.fps, fallback.quality), (320, 10, 75))
        self.assertLess(fallback.side, primary.side)
        self.assertLess(fallback.fps, primary.fps)
        self.assertLess(fallback.quality, primary.quality)
        self.assertEqual(GIF_PROFILE.name, 'gif_320_10_c96')

    def test_encode_command_uses_profiles_without_upscaling(self):
        source = SourceInfo(6.0, 1920, 1080, frame_rate=30)
        for profile in PROFILES:
            with self.subTest(profile=profile.name):
                cmd, width, height = conversion_command(
                    'ffmpeg', __import__('pathlib').Path('/tmp/source.mp4'), source,
                    gif=False, profile=profile)
                self.assertEqual((width, height), target_size(source, profile.side))
                self.assertLessEqual(max(width, height), profile.side)
                self.assertIn(f'fps={profile.fps},', cmd[cmd.index('-vf') + 1])
                self.assertIn('-quality', cmd)
                self.assertEqual(cmd[cmd.index('-quality') + 1], str(profile.quality))
                self.assertEqual(cmd[cmd.index('-frames:v') + 1], str(MAX_FRAMES))
                self.assertIn('-loop', cmd)
                self.assertEqual(cmd[cmd.index('-loop') + 1], '0')

    def test_tiny_animations_keep_original_frames(self):
        source = SourceInfo(0.08, 320, 240, frame_count=3, frame_rate=37.5)
        self.assertTrue(source.preserve_short_frames)
        cmd, _, _ = conversion_command(
            'ffmpeg', __import__('pathlib').Path('/tmp/short.mp4'), source,
            gif=False, profile=PROFILES[0])
        self.assertNotIn('fps=', cmd[cmd.index('-vf') + 1])
        self.assertIn('-enc_time_base', cmd)

    def test_fallback_gif_palette_not_changed(self):
        source = SourceInfo(2.0, 640, 480, frame_rate=24)
        cmd, width, height = conversion_command(
            'ffmpeg', __import__('pathlib').Path('/tmp/source.mp4'), source,
            gif=True, profile=GIF_PROFILE)
        self.assertEqual(max(width, height), 320)
        self.assertIn('max_colors=96', cmd[cmd.index('-filter_complex') + 1])
        self.assertIn('gif', cmd)


if __name__ == '__main__':
    unittest.main()
