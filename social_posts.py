"""Read public X/TikTok post text and optionally translate it into Russian.

No paid APIs. FxTwitter provides X metadata/translation. TikTok uses public
metadata (oEmbed, then yt-dlp fallback) and a limited free MyMemory API.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import html
from html.parser import HTMLParser
import json
import logging
import re
from pathlib import Path
import tempfile
import threading
from urllib.parse import urljoin, urlsplit

import aiohttp
try:
    from langdetect import DetectorFactory, LangDetectException, detect
except ImportError:
    DetectorFactory = None
    LangDetectException = ValueError
    detect = None

from link_utils import X_HOSTS, TIKTOK_HOSTS, is_supported_url, to_embed_url
from media import run_media
from protected_text import mask_tokens, preserves_tokens, restore_tokens
from safe_http import allowed_url

log = logging.getLogger(__name__)
detector_lock = threading.Lock()
if DetectorFactory is not None:
    DetectorFactory.seed = 0

MAX_TEXT = 1700          # Keep original + translated text inside Discord embed limits
MAX_TRANSLATE_BYTES = 2400
CHUNK_BYTES = 450       # MyMemory's API limits each 'q' to 500 UTF-8 bytes
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=12)


@dataclass(frozen=True)
class SocialPost:
    platform: str
    author: str
    handle: str
    original: str
    translation: str | None
    has_media: bool
    # translation_unavailable=True only when text needs translation but it failed
    translation_unavailable: bool = False
    image_urls: tuple[str, ...] = ()
    language: str = ''
    video_url: str = ''
    has_video: bool = False
    media_complete: bool = True
    caption_known_empty: bool = False
    is_gif: bool = False


def truncate(text: str, max_chars: int = MAX_TEXT) -> str:
    text = text.strip()
    return text if len(text) <= max_chars else text[:max_chars - 1].rstrip() + '…'


def normalize_text(value: object) -> str:
    return re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', '', html.unescape(value)).strip() if isinstance(value, str) else ''


def post_id_from_x(url: str) -> str:
    """Extract a validated numeric post ID; never build an HTTP URL from user host."""
    if not is_supported_url(url) or urlsplit(url).hostname.lower() not in X_HOSTS:
        raise ValueError('Not a supported X URL')
    match = re.search(r'/status/(\d+)', urlsplit(url).path, re.I)
    if match is None:
        raise ValueError('Missing status ID')
    return match.group(1)


def x_caption_known_empty(status: dict) -> bool:
    """Confirm a captionless X post, not missing text: only media facets remain.

    FxTwitter's display text omits the media's t.co URL. Cross-check raw_text
    using its UTF-16 facet offsets before allowing an empty caption to be complete.
    """
    if status.get('text') != '':
        return False
    raw = status.get('raw_text')
    if not isinstance(raw, dict) or not isinstance(raw.get('text'), str) or not isinstance(raw.get('facets'), list):
        return False
    try:
        encoded = raw['text'].encode('utf-16-le')
        spans = []
        for facet in raw['facets']:
            if not isinstance(facet, dict) or facet.get('type') != 'media':
                return False
            indices = facet.get('indices')
            if (not isinstance(indices, list) or len(indices) != 2
                    or any(type(index) is not int for index in indices)):
                return False
            start, end = indices
            if not 0 <= start < end <= len(encoded) // 2:
                return False
            spans.append((start * 2, end * 2))
        boundary = len(encoded)
        for start, end in sorted(spans, reverse=True):
            if end > boundary:
                return False
            encoded = encoded[:start] + encoded[end:]
            boundary = start
        return not encoded.decode('utf-16-le').strip()
    except UnicodeError:
        return False


def parse_x_data(data: dict) -> SocialPost | None:
    """Accept FxTwitter v2 `status` or legacy v1 `tweet` schema."""
    if not isinstance(data, dict) or data.get('code', 200) not in (200, '200'):
        return None
    status = data.get('status') or data.get('tweet')
    if not isinstance(status, dict) or status.get('type') == 'tombstone':
        return None
    original = normalize_text(status.get('text'))
    user = status.get('author') if isinstance(status.get('author'), dict) else {}
    if user.get('protected') is True:
        return None
    handle = normalize_text(user.get('screen_name'))
    author = normalize_text(user.get('name')) or handle or 'Автор X'
    media = status.get('media') or {}
    has_media = any(bool(media.get(key)) for key in ('all', 'videos', 'photos', 'animated', 'gifs')) if isinstance(media, dict) else bool(media)
    if not original and not has_media:
        return None
    photos = media.get('photos', []) if isinstance(media, dict) else []
    images = tuple(p['url'] for p in photos[:10] if isinstance(p, dict)
                   and isinstance(p.get('url'), str) and allowed_url(p['url'], media=True)) if isinstance(photos, list) else ()
    videos = media.get('videos') if isinstance(media, dict) else None
    all_media = media.get('all', []) if isinstance(media, dict) else []
    has_video = bool(videos) or (isinstance(all_media, list) and any(
        isinstance(item, dict) and item.get('type') in {'video', 'gif', 'animated_gif'} for item in all_media))
    video_url = ''
    is_gif = False
    if isinstance(videos, list) and videos and isinstance(videos[0], dict):
        is_gif = videos[0].get('type') in {'gif', 'animated_gif'}
        formats = videos[0].get('formats')
        formats = formats if isinstance(formats, list) else []
        # Lower-bitrate H.264 is the most likely to fit a small Discord upload.
        candidates = [v for v in formats if isinstance(v, dict) and v.get('container') == 'mp4'
                      and v.get('codec') == 'h264' and isinstance(v.get('url'), str)
                      and allowed_url(v['url'], media=True)]
        candidates.sort(key=lambda v: v.get('bitrate') if isinstance(v.get('bitrate'), (int, float)) else float('inf'))
        if candidates:
            video_url = candidates[0]['url']
        if is_gif and isinstance(videos[0].get('url'), str) and allowed_url(videos[0]['url'], media=True):
            video_url = videos[0]['url']  # X GIFs are supplied as source MP4s, not .gif files.
    elif isinstance(all_media, list):
        selected = next((item for item in all_media if isinstance(item, dict)
                         and item.get('type') in {'video', 'gif', 'animated_gif'}), None)
        if selected and selected.get('type') in {'gif', 'animated_gif'}:
            is_gif = True
            if isinstance(selected.get('url'), str) and allowed_url(selected['url'], media=True):
                video_url = selected['url']
    media_complete = (isinstance(photos, list) and len(images) == len(photos)
                      and (not isinstance(videos, list) or len(videos) <= 1)
                      and len(images) + int(has_video) <= 10)
    if isinstance(all_media, list):
        video_count = sum(isinstance(item, dict) and item.get('type') in {'video', 'gif', 'animated_gif'}
                          for item in all_media)
        photo_count = sum(isinstance(item, dict) and item.get('type') == 'photo' for item in all_media)
        media_complete = media_complete and video_count <= 1 and photo_count <= len(images)
    trans_obj = status.get('translation')
    target = normalize_text(trans_obj.get('target_lang')).lower() if isinstance(trans_obj, dict) else ''
    translation = normalize_text(trans_obj.get('text')) if isinstance(trans_obj, dict) and target in ('ru', 'ru-ru') else None
    if translation and translation.casefold() == original.casefold():
        translation = None
    if translation and not preserves_tokens(original, translation):
        translation = None
    language = normalize_text(status.get('lang'))
    return SocialPost(
        platform='X / Twitter', author=author, handle=handle,
        original=original, translation=translation or None,
        has_media=has_media,
        translation_unavailable=bool(original) and not language.lower().startswith('ru') and not bool(translation),
        image_urls=images, language=language.lower(), video_url=video_url,
        has_video=has_video, media_complete=media_complete,
        caption_known_empty=x_caption_known_empty(status),
        is_gif=is_gif,
    )


def parse_tiktok_data(data: dict, url: str) -> SocialPost | None:
    if not isinstance(data, dict):
        return None
    # oEmbed: title is often the caption. yt-dlp: description is usually richer.
    text = normalize_text(data.get('description')) or normalize_text(data.get('title'))
    # yt-dlp sometimes returns autogenerated generic titles when there is no caption.
    if re.fullmatch(r'TikTok video #\d+', text, re.I):
        text = ''
    if not text and not (data.get('author_name') or data.get('uploader') or data.get('embed_product_id')):
        return None
    author = normalize_text(data.get('author_name')) or normalize_text(data.get('uploader')) or 'Автор TikTok'
    handle = normalize_text(data.get('uploader_id')) or ''
    if not handle:
        match = re.search(r'/@([^/]+)/', urlsplit(url).path)
        handle = match.group(1) if match else ''
    return SocialPost(platform='TikTok', author=author, handle=handle,
                      original=text, translation=None, has_media=True,
                      has_video='/photo/' not in urlsplit(url).path,
                      media_complete='/photo/' not in urlsplit(url).path)


def detect_source_language(text: str) -> str | None:
    text = text[:2000]
    # Pure emoji/hashtags aren't useful to translate.
    if len(re.findall(r'[^\W\d_]', text, flags=re.UNICODE)) < 4:
        return None
    if detect is None:
        # Safe fallback if langdetect wasn't installed: no ambitious guessing.
        if re.search(r'[іїєґІЇЄҐ]', text):
            return 'uk'
        if re.search(r'[а-яА-ЯёЁ]', text):
            return 'ru'
        if re.search(r'\b(hello|friend|the|and|this|that|new|update|our|you|with)\b',
                     text, flags=re.IGNORECASE):
            return 'en'
        return None
    try:
        # langdetect lazily builds a sizeable global profile factory. Serialize
        # initialization/detection so two metadata requests cannot build it twice.
        with detector_lock:
            language = detect(text)
    except LangDetectException:
        return None
    if language.startswith('ru'):
        return 'ru'
    # MyMemory accepts ISO language identifiers, not arbitrary user text.
    if re.fullmatch(r'[a-z]{2,3}(?:-[a-z]{2})?', language):
        return language
    return None


def chunk_utf8(text: str, max_bytes: int = CHUNK_BYTES) -> list[str]:
    """Split at words when possible without exceeding MyMemory's 500-byte limit."""
    pieces = re.findall(r'\S+\s*', text)
    result: list[str] = []
    current = ''
    for piece in pieces:
        # A very long word (e.g. URL) may need splitting at characters.
        while len(piece.encode('utf-8')) > max_bytes:
            if current:
                result.append(current.rstrip())
                current = ''
            part = ''
            for ch in piece:
                if len((part + ch).encode('utf-8')) > max_bytes:
                    break
                part += ch
            if not part:
                break
            result.append(part)
            piece = piece[len(part):]
        if not piece:
            continue
        if len((current + piece).encode('utf-8')) > max_bytes:
            if current:
                result.append(current.rstrip())
            current = piece
        else:
            current += piece
    if current.strip():
        result.append(current.strip())
    return result


async def fetch_json(session: aiohttp.ClientSession, url: str, *, params: dict | None = None) -> dict | None:
    if not allowed_url(url):
        return None
    try:
        async with session.get(url, params=params, timeout=REQUEST_TIMEOUT,
                               allow_redirects=False) as response:
            if response.status != 200:
                log.info('Public metadata request returned %s from %s', response.status, urlsplit(url).hostname)
                return None
            payload = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                payload.extend(chunk)
                if len(payload) > 1024 * 1024:
                    return None
            data = json.loads(payload)
            return data if isinstance(data, dict) else None
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, UnicodeError) as exc:
        log.info('Metadata fetch failed (%s): %s', urlsplit(url).hostname, type(exc).__name__)
        return None


async def mymemory_translation(session: aiohttp.ClientSession, text: str, source: str,
                               email: str = '') -> str | None:
    """Best-effort translation with free public quota, not guaranteed or unlimited."""
    if source == 'ru' or not re.fullmatch(r'[a-z]{2,3}(?:-[a-z]{2})?', source):
        return None
    # Do not label a silently truncated fragment as translation of the full post.
    if len(text) > MAX_TEXT or len(text.encode('utf-8')) > MAX_TRANSLATE_BYTES:
        return None
    masked, protected = mask_tokens(text)
    chunks = chunk_utf8(masked)
    if not chunks or len(chunks) > 6:
        return None
    output = []
    for chunk in chunks:
        params = {'q': chunk, 'langpair': f'{source}|ru'}
        if email:
            params['de'] = email
        data = await fetch_json(session, 'https://api.mymemory.translated.net/get', params=params)
        if not data or str(data.get('responseStatus')) != '200' or data.get('quotaFinished'):
            return None
        result = data.get('responseData')
        translated = normalize_text(result.get('translatedText')) if isinstance(result, dict) else ''
        if not translated:
            return None
        output.append(translated)
    combined = restore_tokens(' '.join(output), protected)
    if not combined or not preserves_tokens(text, combined):
        return None
    unchanged = re.sub(r'\s+', ' ', combined).casefold() == re.sub(r'\s+', ' ', text).casefold()
    return None if unchanged else combined


async def tiktok_ytdlp_metadata(url: str) -> dict | None:
    """Metadata only: no download, bounded runtime, no shell or arbitrary URL."""
    if not is_supported_url(url):
        return None
    try:
        with tempfile.TemporaryDirectory(prefix='social_metadata_') as folder:
            return await run_media(url, Path(folder), download=False)
    except (TimeoutError, RuntimeError, OSError, ValueError):
        return None


class OpenGraph(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = {}
        self.images = []

    def handle_starttag(self, tag, attrs):
        if tag == 'meta':
            attributes = dict(attrs)
            key = attributes.get('property') or attributes.get('name')
            if key and isinstance(attributes.get('content'), str):
                self.tags[key] = attributes['content']
                if key == 'og:image' and len(self.images) < 4:
                    self.images.append(attributes['content'])


async def tiktok_proxy_metadata(session, url: str) -> SocialPost | None:
    """Read only Open Graph text; never run HTML, scripts or refresh redirects."""
    try:
        async with session.get(to_embed_url(url), timeout=REQUEST_TIMEOUT,
                               allow_redirects=False, headers={'User-Agent': 'Discordbot/2.0'}) as response:
            if response.status != 200:
                return None
            body = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                body.extend(chunk)
                if len(body) > 512 * 1024:
                    return None
        parser = OpenGraph()
        parser.feed(body.decode('utf-8', errors='replace'))
        tags = parser.tags
        caption = tags.get('og:description', '')
        author = tags.get('og:title', '')
        origin = tags.get('og:url', '')
        # Reject error pages and unrelated posts returned by the proxy.
        if not is_supported_url(origin) or urlsplit(origin).hostname not in TIKTOK_HOSTS:
            return None
        expected = re.search(r'/(?:video|photo)/([0-9]+)', urlsplit(url).path)
        actual = re.search(r'/(?:video|photo)/([0-9]+)', urlsplit(origin).path)
        if expected and (not actual or expected.group(1) != actual.group(1)):
            return None
        post = parse_tiktok_data({'title': caption, 'author_name': author}, origin)
        if post is None:
            return None
        video = tags.get('og:video', '')
        video = video if allowed_url(video, media=True) else ''
        images = tuple(image for image in parser.images if allowed_url(image, media=True))
        if video and '/photo/' not in urlsplit(url).path:
            images = ()  # For a video, og:image is a poster, not a separate photo attachment.
        return replace(post, video_url=video, image_urls=images,
                       has_video='/photo/' not in urlsplit(url).path and not (images and not video),
                       media_complete=bool(video) and not images)
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return None


async def resolve_tiktok_url(session, url: str) -> str | None:
    """Resolve only TikTok short links, validating every redirect before requesting."""
    parsed = urlsplit(url)
    if parsed.hostname not in {'vm.tiktok.com', 'vt.tiktok.com'} and not parsed.path.startswith('/t/'):
        return url
    try:
        for _ in range(4):
            if not allowed_url(url) or urlsplit(url).hostname not in TIKTOK_HOSTS:
                return None
            async with session.get(url, allow_redirects=False, timeout=REQUEST_TIMEOUT) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    url = urljoin(url, response.headers.get('Location', ''))
                    if not is_supported_url(url):
                        return None
                elif response.status == 200 and is_supported_url(url):
                    return url
                else:
                    return None
        return None
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return None


async def fetch_social_post(session: aiohttp.ClientSession, url: str,
                            contact_email: str = '', *, translate: bool = True) -> SocialPost | None:
    if not is_supported_url(url):
        return None
    host = (urlsplit(url).hostname or '').lower()
    if host in X_HOSTS:
        post_id = post_id_from_x(url)
        data = await fetch_json(session, f'https://api.fxtwitter.com/2/status/{post_id}',
                                params={'lang': 'ru'} if translate else None)
        post = parse_x_data(data or {})
        # Translation may be missing even if the original post is available.
        if post and not translate:
            return replace(post, translation=None, translation_unavailable=False)
        if post and post.translation_unavailable:
            lang = post.language
            if not lang or lang == 'und':
                lang = await asyncio.to_thread(detect_source_language, post.original) or ''
            if lang and lang != 'ru':
                translation = await mymemory_translation(session, post.original, lang, contact_email)
                if translation:
                    post = replace(post, translation=translation, translation_unavailable=False)
            elif lang == 'ru':
                post = replace(post, translation_unavailable=False)
        return post

    # TikTok public metadata: try lightweight oEmbed, then yt-dlp metadata.
    url = await resolve_tiktok_url(session, url)
    if not url:
        return None
    oembed = await fetch_json(session, 'https://www.tiktok.com/oembed', params={'url': url})
    post = parse_tiktok_data(oembed or {}, url)
    if not post or not post.original or (post.has_video and not post.video_url):
        proxy = await tiktok_proxy_metadata(session, url)
        if post and proxy:
            post = replace(post, original=post.original or proxy.original,
                           author=post.author if post.author != 'Автор TikTok' else proxy.author,
                           handle=post.handle or proxy.handle,
                           video_url=proxy.video_url, image_urls=post.image_urls or proxy.image_urls,
                           media_complete=post.media_complete and proxy.media_complete)
        else:
            post = proxy or post
    if not post or not post.original:
        metadata = await tiktok_ytdlp_metadata(url)
        extracted = parse_tiktok_data(metadata or {}, url)
        if post and extracted:
            post = replace(post, original=post.original or extracted.original,
                           author=post.author if post.author != 'Автор TikTok' else extracted.author,
                           handle=post.handle or extracted.handle)
        else:
            post = extracted or post
    if not post:
        return None
    if not translate:
        return post
    language = await asyncio.to_thread(detect_source_language, post.original)
    if not language or language == 'ru':
        return post
    translation = await mymemory_translation(session, post.original, language, contact_email)
    return replace(post, translation=translation, translation_unavailable=not bool(translation))
