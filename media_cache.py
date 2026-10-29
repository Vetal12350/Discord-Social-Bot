"""Small, disposable disk cache; never retain media in Python RAM or across restarts."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
import hashlib
import logging
import os
from pathlib import Path
import shutil
import tempfile
import time

log = logging.getLogger(__name__)


class MediaCache:
    def __init__(self, *, maximum=24 * 1024 * 1024, entries=4, ttl=120):
        self.maximum, self.entries, self.ttl = maximum, entries, ttl
        self.items = OrderedDict()
        self.directory = None
        self.janitor = None
        self.lock = asyncio.Lock()

    @staticmethod
    async def disk(function, *args):
        # A cancelled caller must wait for the disk operation before its temporary
        # directory is removed or close() races a still-running cache writer.
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.gather(task, return_exceptions=True)
            raise

    @staticmethod
    def link(source, destination):
        try:
            os.link(source, destination)
        except OSError:
            shutil.copyfile(source, destination)  # Bounded disk-to-disk copy, off the event loop.

    def prune(self):
        now = time.monotonic()
        for key, (expires, path, size) in list(self.items.items()):
            if expires <= now or not path.is_file():
                self.items.pop(key)
                path.unlink(missing_ok=True)

    async def get(self, key, destination: Path, limit: int):
        async with self.lock:
            await self.disk(self.prune)
            item = self.items.get(key)
            if not item or item[2] > limit:
                return None
            path = destination.with_suffix(item[1].suffix)
            try:
                await self.disk(self.link, item[1], path)
            except OSError:
                path.unlink(missing_ok=True)
                return None
            self.items.move_to_end(key)
            log.info('media_cache result=hit kind=%s bytes=%d', key[0], item[2])
            return path

    async def put(self, key, source: Path):
        async with self.lock:
            try:
                await self.disk(self._put, key, source)
            except OSError:
                log.info('media_cache result=skipped reason=disk_error')
                return  # Cache failure must never turn a prepared repost into a failure.
            if self.items and self.janitor is None:
                self.janitor = asyncio.create_task(self.expire(), name='social-media-cache-expiry')

    def _put(self, key, source):
        self.prune()
        size = source.stat().st_size
        if not 0 < size <= self.maximum or self.entries <= 0:
            return
        if self.directory is None:
            self.directory = tempfile.TemporaryDirectory(prefix='social_cache_')
        old = self.items.pop(key, None)
        if old:
            old[1].unlink(missing_ok=True)
        while self.items and (len(self.items) >= self.entries or sum(i[2] for i in self.items.values()) + size > self.maximum):
            _, (_, path, _) = self.items.popitem(last=False)
            path.unlink(missing_ok=True)
        digest = hashlib.sha256(repr(key).encode()).hexdigest()
        path = Path(self.directory.name) / (digest + source.suffix)
        try:
            self.link(source, path)
        except OSError:
            path.unlink(missing_ok=True)
            raise
        self.items[key] = (time.monotonic() + self.ttl, path, size)

    async def expire(self):
        try:
            while True:
                await asyncio.sleep(min(30, self.ttl))
                async with self.lock:
                    await self.disk(self.prune)
        except asyncio.CancelledError:
            pass

    async def close(self):
        if self.janitor:
            self.janitor.cancel()
            await asyncio.gather(self.janitor, return_exceptions=True)
            self.janitor = None
        async with self.lock:
            if self.directory:
                await self.disk(self.directory.cleanup)
                self.directory = None
            self.items.clear()
