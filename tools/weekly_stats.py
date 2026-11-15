"""Aggregate repost metrics from the service journal without exposing raw logs.

Usage on the VPS (read-only, nothing is written to disk):

    journalctl -u discord-social-bot.service -o cat \
        --since "2026-11-13 20:55" --until "2026-11-20 20:55" \
        | python3 tools/weekly_stats.py

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


def platform(key: str) -> str:
    return 'x' if key.startswith('x:') else 'tiktok'


@dataclass
class Stats:
    inputs: set = field(default_factory=set)
    confirmed: set = field(default_factory=set)
    skipped: set = field(default_factory=set)
    http_403_by_phase: Counter = field(default_factory=Counter)
    retry_successes: set = field(default_factory=set)
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
            _key, _mode, phase, status = match.groups()
            if status == 'http_403':
                self.http_403_by_phase[phase] += 1
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


def collect(lines: Iterable[str]) -> Stats:
    stats = Stats()
    for line in lines:
        stats.feed(line)
    return stats


def main(stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> int:
    for line in collect(stdin).report():
        print(line, file=stdout)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
