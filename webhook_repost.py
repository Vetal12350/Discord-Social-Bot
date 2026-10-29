"""Channel webhook reuse, flat captions and conservative source-message deletion."""
from __future__ import annotations

import asyncio
from collections import Counter, OrderedDict
from dataclasses import dataclass
import json
import logging
import re
import time

import aiohttp
import discord

from link_utils import canonical_url, is_supported_url
from protected_text import preserves_tokens

WEBHOOK_NAME = 'Social Bot'
NAME_SUFFIX = ' · через Social Bot'
CAPTION_HEADING = '**Текст поста:**\n'
log = logging.getLogger(__name__)


def units(text: str) -> int:
    return len(text.encode('utf-16-le', errors='replace')) // 2


def clip(text: str, limit: int) -> str:
    if units(text) <= limit:
        return text
    return text.encode('utf-16-le', errors='replace')[:(limit - 1) * 2].decode('utf-16-le', errors='ignore') + '…'


def caption(post, *, translate: bool) -> tuple[str, bool]:
    """One final caption; never display a source as if it were a translation."""
    text = post.original
    if (translate and not post.language.startswith('ru') and post.translation
            and preserves_tokens(post.original, post.translation)):
        text = post.translation
    # Keep social handles/URLs readable. Discord pings are disabled in the request.
    if not text.strip():
        return '', True
    rendered = CAPTION_HEADING + discord.utils.escape_markdown(text, ignore_links=True)
    return clip(rendered, 2000), units(rendered) <= 2000


def identity(author) -> tuple[str, str]:
    name = re.sub(r'[\x00-\x1f\x7f]', '', author.display_name).strip() or 'Пользователь'
    return clip(name, 80 - units(NAME_SUFFIX)) + NAME_SUFFIX, str(author.display_avatar.url)


def parent_and_thread(channel):
    if isinstance(channel, discord.Thread):
        if channel.parent is None:
            raise WebhookFailure('Не найден родительский канал ветки.')
        return channel.parent, channel.id
    return channel, None


def source_message_issue(message, source: str) -> tuple[str, str] | None:
    """Explain the existing single-link guard without logging message contents/URLs."""
    raw = message.content.strip()
    if raw.startswith('<') and raw.endswith('>'):
        raw = raw[1:-1]
    if not is_supported_url(raw):
        return 'unsafe_message', 'not_single_supported_url'
    if canonical_url(raw) != canonical_url(source):
        return 'url_mismatch', 'different_normalized_url'
    for attribute in ('attachments', 'stickers', 'poll', 'reference', 'webhook_id'):
        if getattr(message, attribute, None):
            return 'unsafe_message', attribute
    return None


def only_source_message(message, source: str) -> bool:
    """Delete only a message containing exactly one source URL and no other user data."""
    return source_message_issue(message, source) is None


@dataclass(frozen=True)
class Receipt:
    message_id: int
    media_confirmed: bool
    confirmation_reason: str = ''


class WebhookFailure(RuntimeError):
    def __init__(self, message: str, *, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


class WebhookPoster:
    def __init__(self):
        self.cache = OrderedDict()
        self.blocked_until = OrderedDict()
        self.create_lock = asyncio.Lock()

    @staticmethod
    def remember(store, key, value):
        store[key] = value
        store.move_to_end(key)
        while len(store) > 200:
            store.popitem(last=False)

    async def get_webhook(self, channel, bot_user, guild_member):
        parent, thread_id = parent_and_thread(channel)
        if time.monotonic() < self.blocked_until.get(parent.id, 0):
            raise WebhookFailure('Discord временно ограничил Webhook; попробуйте позже.')
        if not parent.permissions_for(guild_member).manage_webhooks:
            raise WebhookFailure('Боту нужно право Manage Webhooks в этом канале.')
        async with self.create_lock:
            webhook = self.cache.get(parent.id)
            if webhook is None:
                hooks = await parent.webhooks()
                webhook = next((hook for hook in hooks
                                if hook.type == discord.WebhookType.incoming and hook.token
                                and hook.user and hook.user.id == bot_user.id
                                and hook.name == WEBHOOK_NAME), None)
                if webhook is None:
                    webhook = await parent.create_webhook(name=WEBHOOK_NAME,
                                                          reason='Social Bot automatic repost')
                self.remember(self.cache, parent.id, webhook)
        return webhook, parent.id, thread_id

    async def execute(self, session, webhook, parent_id, thread_id, channel_id,
                      author, content: str, files: list[discord.File]) -> Receipt:
        """One REST attempt with wait=true; no retry after an ambiguous POST result.

        discord.py's webhook adapter retries 5xx/connection errors, and execute-webhook
        has no nonce deduplication. Use the official REST endpoint once instead.
        Webhook objects/creation/authentication still come from discord.py.
        """
        if not webhook.token:
            raise WebhookFailure('Webhook недоступен; исходное сообщение сохранено.')
        username, avatar = identity(author)
        payload = {'content': content, 'username': username, 'avatar_url': avatar,
                   'allowed_mentions': {'parse': [], 'users': [], 'roles': [], 'replied_user': False},
                   'flags': 4,  # SUPPRESS_EMBEDS: no automatic link cards; attachments stay visible.
                   'attachments': [{'id': index, 'filename': item.filename} for index, item in enumerate(files)]}
        params = {'wait': 'true'}
        if thread_id is not None:
            params['thread_id'] = str(thread_id)
        form = aiohttp.FormData()
        form.add_field('payload_json', json.dumps(payload, ensure_ascii=True), content_type='application/json')
        for index, item in enumerate(files):
            content_type = {'animation.webp': 'image/webp', 'animation.gif': 'image/gif'}.get(
                item.filename, 'application/octet-stream')
            form.add_field(f'files[{index}]', item.fp, filename=item.filename, content_type=content_type)
        # This URL is constructed solely from a Discord-created webhook, never provider/user input.
        endpoint = f'https://discord.com/api/v10/webhooks/{webhook.id}/{webhook.token}'
        try:
            async with session.post(endpoint, params=params, data=form, allow_redirects=False,
                                    headers={'User-Agent': f'DiscordBot (https://github.com/Rapptz/discord.py {discord.__version__}) SocialBot/1.3'},
                                    timeout=aiohttp.ClientTimeout(total=90)) as response:
                body = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    body.extend(chunk)
                    if len(body) > 1024 * 1024:
                        raise WebhookFailure('Ответ Discord не подтверждён; оригинал сохранён.', uncertain=True)
                try:
                    data = json.loads(body)
                except (ValueError, UnicodeError):
                    data = {}
                if response.status == 404:
                    self.cache.pop(parent_id, None)  # Rediscover/create on the next source event only.
                if response.status == 429:
                    delay = data.get('retry_after', 60) if isinstance(data, dict) else 60
                    delay = float(delay) if isinstance(delay, (int, float)) else 60
                    self.remember(self.blocked_until, parent_id, time.monotonic() + max(1, delay))
                if response.status != 200:
                    raise WebhookFailure(f'Discord не подтвердил Webhook (HTTP {response.status}); оригинал сохранён.',
                                         uncertain=response.status >= 500)
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            raise WebhookFailure('Нет подтверждения отправки Webhook; оригинал сохранён.', uncertain=True) from exc
        if (not isinstance(data, dict) or not str(data.get('id', '')).isdigit()
                or int(data['id']) <= 0
                or str(data.get('channel_id')) != str(channel_id)
                or str(data.get('webhook_id')) != str(webhook.id)):
            raise WebhookFailure('Ответ Webhook не подтверждён; оригинал сохранён.', uncertain=True)
        attachments = data.get('attachments')
        actual = [item['filename'] for item in attachments
                  if isinstance(item, dict) and isinstance(item.get('filename'), str)] if isinstance(attachments, list) else []
        expected = [item.filename for item in files]
        confirmed = bool(files) and Counter(actual) == Counter(expected) and data.get('content') == content
        reason = ('no_media' if not files else 'attachment_mismatch' if Counter(actual) != Counter(expected)
                  else 'text_mismatch' if data.get('content') != content else 'confirmed')
        log.info('webhook_confirmation reason=%s expected_files=%d actual_files=%d text_matches=%s',
                 reason, len(expected), len(actual), data.get('content') == content)
        return Receipt(int(data['id']), confirmed, reason)


async def delete_source_if_safe(message, source, receipt, *, media_complete: bool,
                                text_complete: bool, caption_available: bool) -> bool:
    def record(reason, detail='', *, result='skipped', status=None, code=None):
        log.info('delete_source result=%s reason=%s detail=%s message_id=%s channel_id=%s status=%s code=%s',
                 result, reason, detail, message.id, message.channel.id, status, code)

    if not media_complete:
        record('incomplete_media')
        return False
    if not text_complete:
        record('incomplete_text', 'caption_truncated')
        return False
    if not caption_available:
        record('caption_unavailable', 'empty_caption_not_confirmed')
        return False
    if not receipt.media_confirmed:
        record('webhook_unconfirmed', receipt.confirmation_reason)
        return False
    issue = source_message_issue(message, source)
    if issue:
        record(*issue)
        return False
    permissions = message.channel.permissions_for(message.guild.me)
    if not permissions.manage_messages or not permissions.read_message_history:
        missing = ','.join(name for name in ('manage_messages', 'read_message_history') if not getattr(permissions, name))
        record('missing_permissions', missing)
        return False
    # Re-read immediately before deletion: user may have edited/attached content while downloading.
    stage = 'fetch_message'
    try:
        current = await message.channel.fetch_message(message.id)
        if current.content != message.content or current.author.id != message.author.id:
            record('unsafe_message', 'message_changed')
            return False
        issue = source_message_issue(current, source)
        if issue:
            record(*issue)
            return False
        stage = 'delete_message'
        await current.delete()
    except (discord.HTTPException, aiohttp.ClientError, OSError, TimeoutError) as exc:
        record('discord_api_error', stage + ':' + type(exc).__name__,
               status=getattr(exc, 'status', None), code=getattr(exc, 'code', None))
        return False
    record('confirmed', result='deleted')
    return True
