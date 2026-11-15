"""Offline tests for the weekly journal aggregator (synthetic log lines only)."""
import io
import unittest

from tools.weekly_stats import collect, main

P = '2026-11-14 10:00:00,000 INFO discord-video-bot: '


def run(lines):
    return collect(P + line for line in lines)


class WeeklyStatsTests(unittest.TestCase):
    def test_counts_unique_inputs_and_confirmations(self):
        stats = run([
            'tiktok_received message_id=1 channel_id=9 source_keys=tiktok:111,tiktok-short:AbC',
            'tiktok_received message_id=1 channel_id=9 source_keys=tiktok:111',  # duplicate event
            'repost_attempt source_key=tiktok:111 message_id=1 channel_id=9',
            'repost_attempt source_key=x:222 message_id=2 channel_id=9',
            'repost_receipt stage=webhook source_key=tiktok:111 message_id=1 channel_id=9 '
            'media_confirmed=True reason=ok',
            'repost_receipt stage=webhook source_key=tiktok:111 message_id=1 channel_id=9 '
            'media_confirmed=True reason=ok',
            'repost_receipt stage=webhook source_key=x:222 message_id=2 channel_id=9 '
            'media_confirmed=False reason=missing',
        ])
        self.assertEqual(len(stats.inputs), 3)
        self.assertEqual(stats.confirmed, {('1', 'tiktok:111')})

    def test_skips_are_unique_per_reason_and_message(self):
        stats = run([
            'repost skipped reason=global_inflight_limit message_id=5 channel_id=9',
            'repost skipped reason=global_inflight_limit message_id=5 channel_id=9',
            'repost skipped reason=post_inflight source_key=tiktok:1 message_id=6 channel_id=9',
        ])
        self.assertEqual(len(stats.skipped), 2)

    def test_http_403_counted_by_phase(self):
        stats = run([
            'media_worker_http_failure source_key=tiktok:1 mode=download phase=media_get status=http_403 '
            'retry=scheduled retry_reason=none',
            'media_worker_http_failure source_key=tiktok:1 mode=download phase=metadata_get status=http_403 '
            'retry=skipped retry_reason=first_party_403',
            'media_worker_http_failure source_key=tiktok:1 mode=download phase=media_get status=http_404 '
            'retry=skipped retry_reason=status_not_retryable',
        ])
        self.assertEqual(dict(stats.http_403_by_phase), {'media_get': 1, 'metadata_get': 1})

    def test_retry_success_requires_retry_before_confirmed_receipt(self):
        stats = run([
            'repost_attempt source_key=tiktok:1 message_id=1 channel_id=9',
            'media_worker_retry source_key=tiktok:1 mode=download reason=http_503 phase=media_get delay_ms=1000',
            'repost_receipt stage=webhook source_key=tiktok:1 message_id=1 channel_id=9 '
            'media_confirmed=True reason=ok',
            # Later attempt of the same post without retry must not count.
            'repost_attempt source_key=tiktok:1 message_id=2 channel_id=9',
            'repost_receipt stage=webhook source_key=tiktok:1 message_id=2 channel_id=9 '
            'media_confirmed=True reason=ok',
            # Retry followed by failure does not count.
            'repost_attempt source_key=tiktok:3 message_id=3 channel_id=9',
            'media_worker_retry source_key=tiktok:3 mode=download reason=http_429 phase=media_get delay_ms=1000',
            'repost_failure stage=video_prepare error_type=RuntimeError source_key=tiktok:3 '
            'message_id=3 channel_id=9',
        ])
        self.assertEqual(stats.retry_successes, {('1', 'tiktok:1')})

    def test_report_never_contains_ids_or_unrelated_text(self):
        out = io.StringIO()
        lines = [P + 'tiktok_received message_id=123456789012345678 channel_id=876543210987654321 '
                 'source_keys=tiktok:7707467025265757460\n',
                 P + 'repost_attempt source_key=x:999888777 message_id=55555 channel_id=1 '
                 'https://cdn.example/secret?token=DO_NOT_LOG\n',
                 'DISCORD_TOKEN=DO_NOT_LOG\n',
                 'x' * 10000 + ' repost_attempt source_key=x:1 message_id=1 \n']
        main(io.StringIO(''.join(lines)), out)
        text = out.getvalue()
        for secret in ('123456789012345678', '876543210987654321', '7707467025265757460',
                       '999888777', '55555', 'DO_NOT_LOG', 'cdn.example'):
            self.assertNotIn(secret, text)
        self.assertIn('Уникальные входы: 2 (TikTok 1, X 1)', text)
        self.assertEqual(len(text.splitlines()), 5)

    def test_malformed_lines_are_ignored(self):
        stats = run(['tiktok_received message_id=abc channel_id=9 source_keys=tiktok:1',
                     'repost_attempt source_key=https://x.com/a message_id=1 channel_id=9',
                     'repost_receipt stage=webhook source_key=tiktok:1 message_id=1 channel_id=9 '
                     'media_confirmed=Maybe reason=ok'])
        self.assertEqual((len(stats.inputs), len(stats.confirmed)), (0, 0))


if __name__ == '__main__':
    unittest.main()
