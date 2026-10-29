"""Small HTTPS policy: allowlisted hosts, public DNS, no automatic redirects."""
from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlsplit

import aiohttp

API_HOSTS = {'api.fxtwitter.com', 'www.tiktok.com', 'api.mymemory.translated.net',
             'tnktok.com', 'vm.tnktok.com', 'vt.tnktok.com',
             'tiktok.com', 'vm.tiktok.com', 'vt.tiktok.com', 'm.tiktok.com'}
MEDIA_DOMAINS = ('twimg.com', 'tiktok.com', 'tiktokv.com', 'tiktokcdn.com',
                 'tiktokcdn-us.com', 'tiktokcdn-eu.com', 'muscdn.com',
                 'byteoversea.com', 'ibytedtos.com', 'ibyteimg.com', 'byteimg.com',
                 'twitter.com', 'x.com', 'syndication.twimg.com')


def public_ip(value: str) -> bool:
    address = ipaddress.ip_address(value.split('%')[0])
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_global


def allowed_url(url: str, *, media: bool = False) -> bool:
    try:
        p = urlsplit(url)
        if (p.scheme != 'https' or p.username or p.password or p.port
                or re.search(r'[\s\\\x00-\x1f\x7f]', url)):
            return False
        host = (p.hostname or '').lower()
        if media:
            return host == 'offload.tnktok.com' or any(host == d or host.endswith('.' + d) for d in MEDIA_DOMAINS)
        return host in API_HOSTS
    except ValueError:
        return False


class PublicResolver(aiohttp.abc.AbstractResolver):
    def __init__(self) -> None:
        self.resolver = aiohttp.resolver.ThreadedResolver()

    async def resolve(self, host, port=0, family=socket.AF_INET):
        addresses = await self.resolver.resolve(host, port, family)
        if not addresses or any(not public_ip(a['host']) for a in addresses):
            raise OSError('Non-public DNS destination rejected')
        return addresses

    async def close(self):
        await self.resolver.close()


def make_session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(resolver=PublicResolver(), limit=6),
        timeout=aiohttp.ClientTimeout(total=12), trust_env=False,
        headers={'User-Agent': 'DiscordSocialPreviewBot/1.2'},
    )
