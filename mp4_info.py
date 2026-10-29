"""Read bounded MP4 track metadata without decoding or loading the video into RAM."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import struct


class InvalidMP4(ValueError):
    pass


@dataclass(frozen=True)
class MP4Info:
    video_codecs: tuple[str, ...]
    audio_codecs: tuple[str, ...]

    @property
    def compatible(self) -> bool:
        return (bool(self.video_codecs) and all(c in {'avc1', 'avc3'} for c in self.video_codecs)
                and all(c == 'mp4a' for c in self.audio_codecs))


def inspect_mp4(path: Path) -> MP4Info:
    """Check box boundaries and H.264/AAC sample entries; this is not a decoder."""
    size = path.stat().st_size
    budget = 4096
    video = []
    audio = []
    with path.open('rb') as stream:
        def read(offset, length):
            stream.seek(offset)
            value = stream.read(length)
            if len(value) != length:
                raise InvalidMP4('Truncated MP4 metadata')
            return value

        def boxes(start, end):
            nonlocal budget
            while start < end:
                budget -= 1
                if budget < 0 or end - start < 8:
                    raise InvalidMP4('Invalid MP4 box count or size')
                length, kind = struct.unpack('>I4s', read(start, 8))
                header = 8
                if length == 1:
                    if end - start < 16:
                        raise InvalidMP4('Truncated extended box')
                    length = struct.unpack('>Q', read(start + 8, 8))[0]
                    header = 16
                elif length == 0:
                    length = end - start
                if length < header or start + length > end:
                    raise InvalidMP4('Invalid MP4 box boundaries')
                yield kind, start + header, start + length
                start += length

        def children(parent, kind):
            return [(start, end) for box, start, end in boxes(*parent) if box == kind]

        top = list(boxes(0, size))
        if (not any(kind == b'ftyp' and end - start >= 8 for kind, start, end in top)
                or not any(kind == b'mdat' and end > start for kind, start, end in top)):
            raise InvalidMP4('Missing MP4 header or media')
        for kind, start, end in top:
            if kind != b'moov':
                continue
            for track in children((start, end), b'trak'):
                for mdia in children(track, b'mdia'):
                    handlers = children(mdia, b'hdlr')
                    if not handlers or handlers[0][1] - handlers[0][0] < 12:
                        raise InvalidMP4('Missing MP4 track handler')
                    handler = read(handlers[0][0] + 8, 4)
                    entries = []
                    for minf in children(mdia, b'minf'):
                        for stbl in children(minf, b'stbl'):
                            for stsd in children(stbl, b'stsd'):
                                if stsd[1] - stsd[0] < 8:
                                    raise InvalidMP4('Truncated sample description')
                                count = struct.unpack('>I', read(stsd[0] + 4, 4))[0]
                                codecs = [c.decode('ascii', errors='replace')
                                          for c, _, _ in boxes(stsd[0] + 8, stsd[1])]
                                if count != len(codecs):
                                    raise InvalidMP4('Invalid sample entry count')
                                entries.extend(codecs)
                    if handler in {b'vide', b'soun'} and not entries:
                        raise InvalidMP4('Missing codec metadata')
                    if handler == b'vide':
                        video.extend(entries)
                    elif handler == b'soun':
                        audio.extend(entries)
    if not video:
        raise InvalidMP4('No MP4 video track')
    return MP4Info(tuple(video), tuple(audio))
