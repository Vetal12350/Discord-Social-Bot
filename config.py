"""Validate environment configuration before connecting to Discord."""
from __future__ import annotations

from dataclasses import dataclass, field
import os
import re


def snowflake(value: str, name: str) -> int:
    if not re.fullmatch(r'[0-9]{1,20}', value) or not 0 < int(value) < 2**64:
        raise ValueError(f'{name}: нужен положительный Discord ID')
    return int(value)


@dataclass(frozen=True)
class Config:
    token: str = field(default='', repr=False)
    guild_id: int | None = None
    channels: frozenset[int] = frozenset()
    translate: bool = True
    contact_email: str = ''
    repost_mode: str = 'webhook'

    @classmethod
    def from_env(cls):
        token = (os.getenv('DISCORD_TOKEN') or os.getenv('DISCORD_BOT_TOKEN', '')).strip()
        if not token or token == 'PASTE_YOUR_TOKEN_HERE':
            raise ValueError('Задайте DISCORD_TOKEN (или DISCORD_BOT_TOKEN) в .env или окружении')
        guild = os.getenv('TEST_GUILD_ID', '').strip()
        channels = os.getenv('ALLOWED_CHANNEL_IDS', '')
        translate = os.getenv('TRANSLATE_ENABLED', 'true').strip().lower()
        if translate not in {'true', 'false', '1', '0', 'yes', 'no'}:
            raise ValueError('TRANSLATE_ENABLED: используйте true или false')
        email = os.getenv('MYMEMORY_EMAIL', '').strip()
        mode = os.getenv('REPOST_MODE', 'webhook').strip().lower()
        if mode not in {'webhook', 'reply'}:
            raise ValueError('REPOST_MODE: используйте webhook или reply')
        if email and (len(email) > 254 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email)):
            raise ValueError('MYMEMORY_EMAIL: неверный формат email')
        return cls(token, snowflake(guild, 'TEST_GUILD_ID') if guild else None,
                   frozenset(snowflake(v.strip(), 'ALLOWED_CHANNEL_IDS')
                             for v in channels.split(',') if v.strip()),
                   translate in {'true', '1', 'yes'}, email, mode)
