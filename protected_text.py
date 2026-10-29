"""Preserve URLs, hashtags and social/Discord mentions across translation."""
from __future__ import annotations

import re

TOKEN_RE = re.compile(r'(?:https?://|www\.)[^\s<>]+|<@(?:!|&)?[0-9]+>|[@#][\w.]+', re.UNICODE | re.IGNORECASE)


def tokens(text: str) -> list[str]:
    return TOKEN_RE.findall(text)


def preserves_tokens(original: str, translation: str) -> bool:
    return tokens(original) == tokens(translation)


def mask_tokens(text: str) -> tuple[str, dict[str, str]]:
    prefix = 'ZXQKEEP'
    while prefix in text:
        prefix += 'Q'
    values = {}
    def replace(match):
        key = f'{prefix}{len(values)}ZXQ'
        values[key] = match.group(0)
        return key
    return TOKEN_RE.sub(replace, text), values


def restore_tokens(text: str, values: dict[str, str]) -> str | None:
    for key, value in values.items():
        if text.count(key) != 1:
            return None
        text = text.replace(key, value)
    return text
