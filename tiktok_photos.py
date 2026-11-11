"""Read a public TikTok photo post's actual slide images, not its cover.

Only first-party TikTok HTML is fetched. No JavaScript, cookies, browser
profiles, additional domains, or unknown redirects. The returned CDN image
URLs are vetted by the existing safe_http policy before any transfer.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

import aiohttp

from link_utils import TIKTOK_HOSTS, is_supported_url
from safe_http import allowed_url

MAX_HTML_BYTES = 2 * 1024 * 1024
MAX_SLIDES = 10  # One Discord webhook message supports at most 10 files.
PHOTO_PATH = re.compile(r"/@[A-Za-z0-9_.-]{1,100}/photo/([0-9]{1,20})/?", re.I)
REHYDRATION = re.compile(
    r'<script\b(?=[^>]*\bid=["\']__UNIVERSAL_DATA_FOR_REHYDRATION__["\'])[^>]*>(.*?)</script\s*>',
    re.I | re.S,
)
SIGI_STATE = re.compile(
    r'<script\b(?=[^>]*\bid=["\']SIGI_STATE["\'])[^>]*>(.*?)</script\s*>',
    re.I | re.S,
)


def parse_photo_html(page: str, post_id: str) -> dict | None:
    """Return complete ordered slides only for the *requested* public post."""
    item = None
    for pattern in (REHYDRATION, SIGI_STATE):
        match = pattern.search(page)
        if match is None:
            continue
        try:
            data = json.loads(match.group(1))
            if pattern is REHYDRATION:
                detail = data['__DEFAULT_SCOPE__']['webapp.video-detail']
                if detail.get('statusCode') not in (0, None):
                    continue
                item = detail['itemInfo']['itemStruct']
            else:
                item = data['ItemModule'][post_id]
        except (KeyError, TypeError, ValueError):
            continue
        if isinstance(item, dict):
            break
    if not isinstance(item, dict) or str(item.get('id')) != post_id:
        return None

    image_post = item.get('imagePost')
    images = image_post.get('images') if isinstance(image_post, dict) else None
    if not isinstance(images, list) or not 1 <= len(images) <= MAX_SLIDES:
        return None

    result = []
    for image in images:
        if not isinstance(image, dict):
            return None
        data = image.get('imageURL')
        links = data.get('urlList') if isinstance(data, dict) else None
        if not isinstance(links, list):
            return None
        selected = next((link for link in links
                         if isinstance(link, str) and allowed_url(link, media=True)), None)
        if not selected:
            return None
        result.append(selected)

    author = item.get('author')
    if not isinstance(author, dict):
        author = {}
    # Read only text/media data from the matching item, never a poster thumbnail.
    caption = item.get('desc') if isinstance(item.get('desc'), str) else ''
    return {
        'description': caption,
        'caption_known_empty': item.get('desc') == '',
        'author': author.get('nickname') if isinstance(author.get('nickname'), str) else '',
        'handle': author.get('uniqueId') if isinstance(author.get('uniqueId'), str) else '',
        'image_urls': tuple(result),
    }


async def fetch_tiktok_photo_post(session: aiohttp.ClientSession, url: str) -> dict | None:
    """Strict first-party TikTok photo-page fetch with a hard HTML size cap."""
    if not is_supported_url(url):
        return None
    parsed = urlsplit(url)
    if parsed.hostname not in TIKTOK_HOSTS:
        return None
    match = PHOTO_PATH.fullmatch(parsed.path)
    if match is None:
        return None
    post_id = match.group(1)
    # As on gallery-dl, /video/<id> can expose the canonical video-detail
    # structure even for photo posts. Try the original /photo/ page as well.
    for path in (parsed.path.replace('/photo/', '/video/', 1), parsed.path):
        target = 'https://www.tiktok.com' + path.rstrip('/')
        if not allowed_url(target, media=True):
            return None
        try:
            async with session.get(
                target, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=12),
                headers={'User-Agent': 'Mozilla/5.0 (compatible; SocialBot/1.0)'},
            ) as response:
                if response.status != 200:
                    continue
                body = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    body.extend(chunk)
                    if len(body) > MAX_HTML_BYTES:
                        break
                else:
                    result = parse_photo_html(body.decode('utf-8', errors='replace'), post_id)
                    if result:
                        return result
        except (aiohttp.ClientError, TimeoutError, ValueError, OSError):
            continue
    return None
