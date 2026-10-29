"""Discord public social previews. Run with Python 3.11+."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
import logging
from pathlib import Path
import tempfile
import time

import aiohttp
import discord
from discord import app_commands
from dotenv import load_dotenv

from config import Config
from link_utils import canonical_url, extract_urls, is_supported_url
from media import MAX_BYTES, MediaTooLarge, download_image, download_mp4, run_media
from media_cache import MediaCache
from process_limits import under_pressure
from mp4_info import InvalidMP4, inspect_mp4
from safe_http import make_session
from social_posts import SocialPost, fetch_social_post
from animation import AnimationError, convert_animation
from webhook_repost import WebhookFailure, WebhookPoster, caption, delete_source_if_safe

log = logging.getLogger('discord-video-bot')
NO_MENTIONS = discord.AllowedMentions.none()


def clip(text: str, limit: int) -> str:
    """Discord counts UTF-16 units, including escaped Markdown characters."""
    raw = text.encode('utf-16-le', errors='replace')
    if len(raw) <= limit * 2:
        return text
    return raw[:(limit - 1) * 2].decode('utf-16-le', errors='ignore').rstrip() + '…'


def safe_text(text: str, limit: int) -> str:
    text = discord.utils.escape_mentions(text).replace('<', '‹').replace('>', '›')
    return clip(discord.utils.escape_markdown(text), limit)


def post_embed(post: SocialPost, source_url: str, *, translate: bool = True) -> discord.Embed:
    embed = discord.Embed(title=safe_text(f'{post.platform} · {post.author}', 240),
                          url=source_url, color=0x5965F2)
    chunks = ['**Оригинал**\n' + (safe_text(post.original, 1650) or 'Подпись недоступна.')]
    if translate and post.translation:
        chunks.append('**🇷🇺 Перевод на русский**\n' + safe_text(post.translation, 1650))
    elif translate and post.translation_unavailable:
        chunks.append('*Перевод недоступен; оригинал сохранён.*')
    embed.description = '\n\n'.join(chunks)
    if post.image_urls:
        embed.set_image(url=post.image_urls[0])
    return embed


def preview(post: SocialPost | None, source: str, *, translate: bool = True,
            embeds_allowed: bool = True) -> dict:
    if post is None:
        return {'content': 'Не удалось получить публикацию. Откройте оригинал: <' + source + '>'}
    if not embeds_allowed:
        text = f'{safe_text(post.platform, 50)} · {safe_text(post.author, 150)}\nОригинал:\n'
        text += safe_text(post.original, 750) or 'Подпись недоступна.'
        if translate and post.translation:
            text += '\nПеревод на русский:\n' + safe_text(post.translation, 750)
        elif translate and post.translation_unavailable:
            text += '\nПеревод недоступен.'
        return {'content': clip(text, 1750) + '\n<' + source + '>'}
    embeds = [post_embed(post, source, translate=translate)]
    for image in post.image_urls[1:4]:
        embeds.append(discord.Embed(url=source).set_image(url=image))
    return {'content': 'Оригинал: <' + source + '>', 'embeds': embeds}


def add_notice(payload: dict, source: str, notice: str) -> dict:
    """Keep the source link and stay within 2000 UTF-16 units, including notices."""
    original = '<' + source + '>'
    body = payload['content'].removesuffix(original).rstrip()
    limit = 1998 - len(original.encode('utf-16-le')) // 2
    return {**payload, 'content': clip(notice + '\n' + body, limit) + '\n' + original}


def remember(store: OrderedDict, key, value, maximum: int = 1000):
    store[key] = value
    store.move_to_end(key)
    while len(store) > maximum:
        store.popitem(last=False)


class VideoBot(discord.Client):
    def __init__(self, config: Config | None = None):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents, allowed_mentions=NO_MENTIONS,
                         max_messages=25, enable_debug_events=False)
        self.config = config or Config()
        self.tree = app_commands.CommandTree(self)
        self.tree.add_command(app_commands.Command(
            name='video', description='Загрузить MP4 из публичного X/Twitter или TikTok',
            callback=self.video))
        self.http_session: aiohttp.ClientSession | None = None
        self.metadata_slots = asyncio.Semaphore(2)
        self.details_inflight = {}
        self.active_posts = set()
        self.media_cache = MediaCache()
        self.post_cache = OrderedDict()
        self.seen_messages = OrderedDict()
        self.auto_requests = OrderedDict()
        self.last_requested = OrderedDict()
        self.active_messages = 0
        self.download_busy = False
        self.media_lock = asyncio.Lock()
        self.webhook_poster = WebhookPoster()

    async def setup_hook(self):
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=2, thread_name_prefix='social-io'))
        self.http_session = make_session()
        try:
            if self.config.guild_id:
                guild = discord.Object(id=self.config.guild_id)
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
            else:
                await self.tree.sync()
            log.info('Slash commands synced')
        except discord.HTTPException as exc:
            log.warning('Slash command sync failed (%s); automatic previews remain enabled', type(exc).__name__)

    async def close(self):
        tasks = list(self.details_inflight.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.media_cache.close()
        if self.http_session is not None:
            await self.http_session.close()
        await super().close()

    async def on_ready(self):
        log.info('Connected to Discord; bot ID %s', self.user.id if self.user else '?')

    async def prepare_video(self, post, source, folder, limit):
        media = folder / 'video.mp4'
        cache_key = ('mp4', canonical_url(source), post.video_url if post else '')
        cached = await self.media_cache.get(cache_key, media, limit)
        if cached:
            return cached, await MediaCache.disk(inspect_mp4, cached)
        if post and post.video_url and self.http_session:
            try:
                await download_mp4(self.http_session, post.video_url, media, limit)
                info = await MediaCache.disk(inspect_mp4, media)
                if info.compatible:
                    await self.media_cache.put(cache_key, media)
                    return media, info
            except MediaTooLarge:
                raise
            except (RuntimeError, OSError, ValueError, TimeoutError, aiohttp.ClientError):
                pass  # Expired URL, bad container or incompatible codec: one bounded extraction attempt.
            media.unlink(missing_ok=True)
        await run_media(source, folder, download=True, limit=limit)
        if not media.is_file() or media.stat().st_size == 0:
            raise RuntimeError('No public MP4 available')
        if media.stat().st_size > limit:
            raise MediaTooLarge('Media size limit exceeded')
        info = await MediaCache.disk(inspect_mp4, media)
        if not info.compatible:
            raise InvalidMP4('H.264 video and AAC audio required')
        await self.media_cache.put(cache_key, media)
        return media, info

    async def send_post(self, post, source, permissions, limit, send, *, force_video=False, flat=False):
        """Send one final reply; hold a single bounded MP4 until Discord has read it."""
        if flat and post:
            text, _ = caption(post, translate=self.config.translate)
            payload = add_notice({'content': '<' + source + '>'}, source, text)
        else:
            payload = preview(post, source, translate=self.config.translate,
                              embeds_allowed=permissions.embed_links)
        if not force_video and not (post and (post.has_video or post.video_url)):
            await send(**payload, allowed_mentions=NO_MENTIONS)
            return
        if not permissions.attach_files:
            payload = add_notice(payload, source, 'Для отправки видео боту нужно право Attach Files.')
            await send(**payload, allowed_mentions=NO_MENTIONS)
            return
        limit = min(MAX_BYTES, limit)
        upload_attempted = False
        try:
            # Active automatic messages are already bounded at six; no unbounded queue or files.
            async with asyncio.timeout(180):
                async with self.media_lock:
                    with tempfile.TemporaryDirectory(prefix='social_video_') as temp:
                        media, info = await self.prepare_video(post, source, Path(temp), limit)
                        upload = dict(payload)
                        if not info.audio_codecs:
                            upload = add_notice(upload, source, 'В полученном видео нет звуковой дорожки.')
                        with closing(discord.File(media, filename='video.mp4')) as attachment:
                            try:
                                upload_attempted = True
                                await send(**upload, file=attachment, allowed_mentions=NO_MENTIONS)
                            except discord.HTTPException as exc:
                                # A definite rejection can safely fall back. Never send a second
                                # reply after an ambiguous connection error / 5xx / cancelled upload.
                                if exc.status not in {400, 403, 413}:
                                    raise
                                reason = ('Discord отклонил вложение (размер, формат или права). '
                                          'Видео не прикреплено.')
                            else:
                                return
        except MediaTooLarge:
            reason = f'Видео больше лимита этой загрузки ({limit / 1024 / 1024:g} МиБ). Видео не прикреплено.'
        except InvalidMP4:
            reason = 'Не удалось получить совместимый MP4 (H.264/AAC). Видео не прикреплено.'
        except (RuntimeError, OSError, ValueError, TimeoutError, aiohttp.ClientError) as exc:
            if upload_attempted:
                raise  # Discord may already have created the message; do not post a duplicate.
            log.info('MP4 unavailable (%s)', type(exc).__name__)
            reason = 'Не удалось скачать совместимый MP4: источник недоступен или истёк срок ожидания.'
        payload = add_notice(payload, source, reason + '\nПосмотрите видео в оригинальной публикации.')
        await send(**payload, allowed_mentions=NO_MENTIONS)

    async def get_details(self, source: str) -> SocialPost | None:
        if self.http_session is None:
            return None
        key = canonical_url(source)
        cached = self.post_cache.get(key)
        if cached and time.monotonic() - cached[0] < (900 if cached[1] else 30):
            return cached[1]
        task = self.details_inflight.get(key)
        if task is None:
            if len(self.details_inflight) >= 6:
                return None
            task = asyncio.create_task(self.fetch_details(source, key), name='social-metadata')
            self.details_inflight[key] = task
        return await asyncio.shield(task)

    async def fetch_details(self, source, key):
        try:
            async with self.metadata_slots:
                try:
                    async with asyncio.timeout(45):
                        post = await fetch_social_post(self.http_session, source,
                                                       self.config.contact_email,
                                                       translate=self.config.translate)
                except Exception as exc:
                    log.warning('Post metadata unavailable (%s)', type(exc).__name__)
                    post = None
                # One cache includes metadata and its protected Russian translation.
                remember(self.post_cache, key, (time.monotonic(), post), 100)
                return post
        finally:
            self.details_inflight.pop(key, None)

    async def repost_message(self, message, post, source, permissions):
        if post is None:
            raise WebhookFailure('Не удалось получить публикацию; исходное сообщение сохранено.')
        if post.has_media and not permissions.attach_files:
            raise WebhookFailure('Для пересылки медиа нужно право Attach Files; оригинал сохранён.')
        content, text_complete = caption(post, translate=self.config.translate)
        sending = False
        try:
            async with asyncio.timeout(180):
                async with self.media_lock:
                    webhook, parent_id, thread_id = await self.webhook_poster.get_webhook(
                        message.channel, self.user, message.guild.me)
                    limit = min(MAX_BYTES, message.guild.filesize_limit)
                    with tempfile.TemporaryDirectory(prefix='social_repost_') as temp, ExitStack() as stack:
                        folder = Path(temp)
                        paths = []
                        used = 0
                        if post.has_video or post.video_url:
                            animation_key = ('animation', canonical_url(source), post.video_url, limit)
                            video = (await self.media_cache.get(animation_key, folder / 'animation.webp', limit)
                                     if post.is_gif else None)
                            cached_animation = video is not None
                            if video is None:
                                video, _ = await self.prepare_video(post, source, folder, limit)
                            if post.is_gif and not cached_animation:
                                animation, details = await convert_animation(video, folder, limit)
                                video.unlink(missing_ok=True)
                                video = animation
                                await self.media_cache.put(animation_key, video)
                                log.info('X GIF converted format=%s frames=%s loop=%s bytes=%s',
                                         details['format'], details['frames'], details['loop'], details['bytes'])
                            paths.append(video)
                            used += video.stat().st_size
                        for index, image in enumerate(post.image_urls[:10 - len(paths)]):
                            remaining = limit - used
                            if remaining <= 0:
                                raise MediaTooLarge('Repost exceeds upload budget')
                            photo_key = ('photo', image)
                            photo = await self.media_cache.get(photo_key, folder / f'photo_{index + 1}.img', remaining)
                            if photo is None:
                                photo = await download_image(self.http_session, image, folder / f'photo_{index + 1}.img', remaining)
                                await self.media_cache.put(photo_key, photo)
                            paths.append(photo)
                            used += photo.stat().st_size
                        if post.has_media and not paths:
                            raise WebhookFailure('Не удалось получить медиа; исходное сообщение сохранено.')
                        if not content and not paths:
                            raise WebhookFailure('У публикации нет доступного текста или медиа.')
                        files = [stack.enter_context(closing(discord.File(path, filename=path.name))) for path in paths]
                        sending = True
                        receipt = await self.webhook_poster.execute(
                            self.http_session, webhook, parent_id, thread_id, message.channel.id,
                            message.author, content, files)
        except WebhookFailure:
            raise
        except AnimationError as exc:
            log.info('GIF conversion failed reason=%s detail=%r attempts=%s',
                     exc.reason, str(exc), exc.details)
            raise WebhookFailure('Не удалось подготовить GIF (FFmpeg, формат или лимит). '
                                 'Исходное сообщение сохранено.') from exc
        except MediaTooLarge as exc:
            raise WebhookFailure('Медиа больше лимита загрузки; исходное сообщение сохранено.') from exc
        except (RuntimeError, OSError, ValueError, TimeoutError, aiohttp.ClientError) as exc:
            raise WebhookFailure('Не удалось подготовить пересылку; исходное сообщение сохранено.', uncertain=sending) from exc
        # A delete failure must never cause a second webhook send.
        try:
            await delete_source_if_safe(message, source, receipt,
                                        media_complete=post.media_complete, text_complete=text_complete,
                                        caption_available=bool(post.original) or post.caption_known_empty)
        except (discord.HTTPException, aiohttp.ClientError, OSError, TimeoutError) as exc:
            log.info('Repost sent; original retained (%s)', type(exc).__name__)

    async def on_message(self, message: discord.Message):
        if (message.author.bot or (self.user and message.author.id == self.user.id)
                or getattr(message, 'webhook_id', None) is not None or not message.guild):
            return
        if self.config.channels and message.channel.id not in self.config.channels:
            return
        urls = extract_urls(message.content)
        if not urls:
            return
        now = time.monotonic()
        if message.id in self.seen_messages:
            return
        permissions = message.channel.permissions_for(message.guild.me)
        in_thread = isinstance(message.channel, discord.Thread)
        if not (permissions.send_messages_in_threads if in_thread else permissions.send_messages):
            return
        if self.active_messages >= 3 or under_pressure() or now - self.auto_requests.get(message.author.id, -100) < 5:
            return
        remember(self.seen_messages, message.id, now, 2000)
        remember(self.auto_requests, message.author.id, now)
        self.active_messages += 1
        try:
            for index, source in enumerate(urls):
                if source in self.active_posts:
                    log.info('repost skipped reason=post_inflight')
                    continue  # Retain this second source message; no partial or duplicate repost.
                self.active_posts.add(source)
                try:
                    post = await self.get_details(source)
                    async def send(**payload):
                        # discord.py sends enforce_nonce=True; retries reuse this nonce.
                        payload['nonce'] = f'{message.id}:{index}'
                        if permissions.read_message_history:
                            return await message.reply(**payload, mention_author=False)
                        return await message.channel.send(**payload)
                    try:
                        if self.config.repost_mode == 'webhook':
                            await self.repost_message(message, post, source, permissions)
                        else:
                            await self.send_post(post, source, permissions, message.guild.filesize_limit, send)
                    except WebhookFailure as exc:
                        log.info('Webhook repost incomplete (uncertain=%s)', exc.uncertain)
                        log.info('delete_source result=skipped reason=webhook_unconfirmed detail=repost_failed '
                                 'message_id=%s channel_id=%s', message.id, message.channel.id)
                        if not exc.uncertain:
                            try:
                                await send(content=str(exc) + '\n<' + source + '>', allowed_mentions=NO_MENTIONS)
                            except (discord.HTTPException, aiohttp.ClientError, OSError, TimeoutError):
                                log.info('Could not send repost failure notice')
                    except discord.NotFound:
                        break
                    except discord.Forbidden:
                        log.warning('Missing channel permissions in %s', message.channel.id)
                        break
                    except (discord.HTTPException, aiohttp.ClientError, OSError, TimeoutError) as exc:
                        log.warning('Preview send failed (%s)', type(exc).__name__)
                finally:
                    self.active_posts.discard(source)
        finally:
            self.active_messages -= 1

    async def video(self, interaction: discord.Interaction, url: str):
        if not interaction.guild or (self.config.channels and interaction.channel_id not in self.config.channels):
            await interaction.response.send_message('Команда доступна только в разрешённом канале сервера.', ephemeral=True)
            return
        if not is_supported_url(url.strip()):
            await interaction.response.send_message('Отправь одну HTTPS-ссылку на публичный пост X/Twitter или TikTok.', ephemeral=True)
            return
        source = canonical_url(url.strip())
        now = time.monotonic()
        if now - self.last_requested.get(interaction.user.id, -100) < 20:
            await interaction.response.send_message('Подожди 20 секунд перед следующей загрузкой.', ephemeral=True)
            return
        if self.download_busy or self.media_lock.locked() or source in self.active_posts or under_pressure():
            await interaction.response.send_message('Сейчас идёт другая загрузка. Попробуй немного позже.', ephemeral=True)
            return
        remember(self.last_requested, interaction.user.id, now)
        self.download_busy = True
        self.active_posts.add(source)
        try:
            await interaction.response.defer(thinking=True)
            post = await self.get_details(source)
            await self.send_post(post, source, interaction.app_permissions,
                                 interaction.filesize_limit, interaction.followup.send, force_video=True, flat=True)
        except (discord.HTTPException, aiohttp.ClientError, OSError, TimeoutError) as exc:
            log.warning('Interaction no longer available (%s)', type(exc).__name__)
        finally:
            self.download_busy = False
            self.active_posts.discard(source)


def main():
    load_dotenv(Path(__file__).with_name('.env'), override=False)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    try:
        config = Config.from_env()
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    client = VideoBot(config)
    client.run(config.token, log_handler=None)


if __name__ == '__main__':
    main()
