"""Aggregate repost metrics from the service journal without exposing raw logs.

Usage on the VPS (read-only, nothing is written to disk):

    journalctl -u discord-social-bot.service -o cat \
        --since "2026-11-13 20:55" --until "2026-11-20 20:55" \
        | python3 tools/weekly_stats.py [--details]

--details adds a failure breakdown by fixed categories (stage, HTTP phase,
status, retry reason, worker reason) and CDN mirror rescues.

Only fixed log markers and allowlisted tokens are parsed. Message IDs and
source keys are kept in memory for de-duplication and are never printed.
Standard library only, so the system Python is enough.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import re
import sys
from typing import Iterable, TextIO

MAX_LINE = 4096
_ID = r'[0-9]{1,20}'
_KEY = r'(?:x:[0-9]{1,25}|tiktok:[0-9]{1,25}|tiktok-short:[A-Za-z0-9_-]{1,32})'

RECEIVED = re.compile(rf'\btiktok_received message_id=({_ID}) channel_id={_ID} '
                      rf'source_keys=({_KEY}(?:,{_KEY}){{0,9}})(?:\s|$)')
ATTEMPT = re.compile(rf'\brepost_attempt source_key=({_KEY}) message_id=({_ID}) ')
RECEIPT = re.compile(rf'\brepost_receipt stage=webhook source_key=({_KEY}) message_id=({_ID}) '
                     rf'channel_id={_ID} media_confirmed=(True|False)\b')
SKIPPED = re.compile(rf'\brepost skipped reason=([a-z_]{{1,40}}) (?:source_key=({_KEY}) )?message_id=({_ID}) ')
HTTP_FAILURE = re.compile(rf'\bmedia_worker_http_failure source_key=({_KEY}) mode=(download|metadata) '
                          r'phase=([a-z_]{1,30}) status=([a-z_0-9]{1,30}) ')
RETRY = re.compile(rf'\bmedia_worker_retry source_key=({_KEY}) mode=(?:download|metadata) ')
FAILURE = re.compile(rf'\brepost_failure stage=([a-z_]{{1,30}}) error_type=([A-Za-z]{{1,40}}) source_key=({_KEY}) ')
WORKER_FAILURE = re.compile(r'\bmedia_worker_failure mode=(download|metadata) reason=([a-z_0-9]{1,40}) ')
MIRROR = re.compile(r'\b(media_worker_mirror|media_download_fallback)\b')


def platform(key: str) -> str:
    return 'x' if key.startswith('x:') else 'tiktok'


@dataclass
class Stats:
    inputs: set = field(default_factory=set)
    confirmed: set = field(default_factory=set)
    skipped: set = field(default_factory=set)
    http_403_by_phase: Counter = field(default_factory=Counter)
    retry_successes: set = field(default_factory=set)
    http_failures: Counter = field(default_factory=Counter)
    failures: Counter = field(default_factory=Counter)
    worker_reasons: Counter = field(default_factory=Counter)
    mirror_rescues: Counter = field(default_factory=Counter)
    _pending_retry: set = field(default_factory=set)

    def feed(self, line: str) -> None:
        if len(line) > MAX_LINE:
            return
        if match := RECEIVED.search(line):
            message_id, keys = match.groups()
            for key in keys.split(','):
                self.inputs.add((message_id, key))
        elif match := ATTEMPT.search(line):
            key, message_id = match.groups()
            # X has no pre-admission marker; TikTok attempts are already inputs
            # unless the log predates tiktok_received.
            self.inputs.add((message_id, key))
            self._pending_retry.discard(key)
        elif match := RECEIPT.search(line):
            key, message_id, confirmed = match.groups()
            if confirmed == 'True':
                self.confirmed.add((message_id, key))
                if key in self._pending_retry:
                    self.retry_successes.add((message_id, key))
            self._pending_retry.discard(key)
        elif match := SKIPPED.search(line):
            reason, key, message_id = match.groups()
            self.skipped.add((reason, message_id, key or ''))
        elif match := HTTP_FAILURE.search(line):
            key, _mode, phase, status = match.groups()
            if status == 'http_403':
                self.http_403_by_phase[phase] += 1
            reason = re.search(r'\bretry_reason=([a-z_0-9]{1,40})\b', line)
            self.http_failures[(platform(key), phase, status, reason.group(1) if reason else '-')] += 1
        elif match := FAILURE.search(line):
            stage, error_type, key = match.groups()
            self.failures[(platform(key), stage, error_type)] += 1
        elif match := WORKER_FAILURE.search(line):
            self.worker_reasons[match.groups()] += 1
        elif match := MIRROR.search(line):
            self.mirror_rescues['video' if match.group(1) == 'media_worker_mirror' else 'slide'] += 1
        elif match := RETRY.search(line):
            self._pending_retry.add(match.group(1))

    def report(self) -> list[str]:
        def split(items: set, key_index: int = 1) -> str:
            counts = Counter(platform(item[key_index]) for item in items)
            return f'(TikTok {counts["tiktok"]}, X {counts["x"]})'

        skip_reasons = Counter(reason for reason, _mid, _key in self.skipped)
        phases = ', '.join(f'{phase} {n}' for phase, n in sorted(self.http_403_by_phase.items())) or '-'
        reasons = ', '.join(f'{reason} {n}' for reason, n in sorted(skip_reasons.items())) or '-'
        return [
            f'Уникальные входы: {len(self.inputs)} {split(self.inputs)}',
            f'Подтверждённые отправки: {len(self.confirmed)} {split(self.confirmed)}',
            f'Пропуски: {len(self.skipped)} [{reasons}]',
            f'HTTP 403 (события worker): {sum(self.http_403_by_phase.values())} [{phases}]',
            f'Успехи после retry: {len(self.retry_successes)} {split(self.retry_successes)}',
        ]

    def details(self) -> list[str]:
        """Failure breakdown by fixed categories only (bounded, no IDs)."""
        def top(counter: Counter, limit: int = 8) -> list[str]:
            return [f'  {n} × ' + ' '.join(key) for key, n in counter.most_common(limit)] or ['  -']
        rescues = ', '.join(f'{kind} {n}' for kind, n in sorted(self.mirror_rescues.items())) or '-'
        return ['Отказы (платформа stage error_type):', *top(self.failures),
                'HTTP-ошибки worker (платформа phase status retry_reason):', *top(self.http_failures),
                'Причины отказа worker (mode reason):', *top(self.worker_reasons),
                f'Спасено зеркалами CDN: {rescues}']


def collect(lines: Iterable[str]) -> Stats:
    stats = Stats()
    for line in lines:
        stats.feed(line)
    return stats


def main(stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout, argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    stats = collect(stdin)
    lines = stats.report() + (stats.details() if '--details' in args else [])
    for line in lines:
        print(line, file=stdout)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
