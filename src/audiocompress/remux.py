"""Re-embed filtered tags + processed cover via mutagen.

Opus/Ogg: METADATA_BLOCK_PICTURE (base64 Picture block), LYRICS for lyrics.
MP3: ID3 text frames + USLT (lyrics) + APIC cover frame.
Cover bytes are reused as-is — no ffmpeg image re-encode involved.
"""

from __future__ import annotations

import base64
import io
from pathlib import Path

from mutagen.flac import Picture


def write_tags_cover(
    path: str | Path,
    tags: dict[str, list[str]],
    cover: tuple[bytes, str] | None,
) -> None:
    from mutagen.oggopus import OggOpus
    from mutagen.oggvorbis import OggVorbis

    p = Path(path)
    suffix = p.suffix.lower()
    if suffix in (".opus", ".ogg"):
        _write_vorbis(p, suffix, tags, cover)
    elif suffix == ".mp3":
        _write_mp3(p, tags, cover)
    else:  # pragma: no cover
        raise ValueError(f"can only remux .opus/.ogg/.mp3, got {suffix}")


def _write_vorbis(
    p: Path, suffix: str, tags: dict[str, list[str]], cover: tuple[bytes, str] | None
) -> None:
    from mutagen.oggopus import OggOpus
    from mutagen.oggvorbis import OggVorbis

    audio = OggOpus(p) if suffix == ".opus" else OggVorbis(p)

    if audio.tags is None:
        audio.addtags()
    # Clear stripped state, then write only what user selected.
    audio.tags.clear()
    for k, vals in tags.items():
        audio.tags[k.lower()] = vals

    if cover:
        data, mime = cover
        pic = Picture()
        pic.type = 3  # front cover
        pic.mime = mime
        pic.desc = "Cover"
        pic.data = data
        try:  # fill dimensions so players don't have to decode
            import warnings

            from PIL import Image

            with warnings.catch_warnings():
                warnings.simplefilter("ignore", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data)) as im:
                    pic.width, pic.height = im.width, im.height
                    pic.depth = 24
        except Exception:
            pass
        block = pic.write()
        audio.tags["metadata_block_picture"] = [
            base64.b64encode(block).decode("ascii")
        ]
    audio.save()


# Normalized key -> ID3 frame. Anything else becomes TXXX:<KEY>.
_ID3_FRAMES = {
    "title": "TIT2",
    "artist": "TPE1",
    "album": "TALB",
    "albumartist": "TPE2",
    "composer": "TCOM",
    "genre": "TCON",
    "date": "TDRC",
    "tracknumber": "TRCK",
    "discnumber": "TPOS",
    "description": "TIT3",
    "lyricist": "TEXT",
    "conductor": "TPE3",
    "label": "TPUB",
}


def _write_mp3(
    p: Path, tags: dict[str, list[str]], cover: tuple[bytes, str] | None
) -> None:
    from mutagen.id3 import (
        APIC,
        COMM,
        TALB,
        TCOM,
        TCON,
        TDRC,
        TEXT,
        TIT2,
        TIT3,
        TPE1,
        TPE2,
        TPE3,
        TPOS,
        TPUB,
        TRCK,
        TXXX,
        USLT,
    )
    from mutagen.mp3 import MP3

    audio = MP3(p)
    if audio.tags is None:
        audio.add_tags()
    id3 = audio.tags
    # Clear stripped state, then write only what user selected.
    id3.clear()

    frame_classes = {
        "TIT2": TIT2,
        "TPE1": TPE1,
        "TALB": TALB,
        "TPE2": TPE2,
        "TCOM": TCOM,
        "TCON": TCON,
        "TDRC": TDRC,
        "TRCK": TRCK,
        "TPOS": TPOS,
        "TIT3": TIT3,
        "TEXT": TEXT,
        "TPE3": TPE3,
        "TPUB": TPUB,
    }

    for key, vals in tags.items():
        k = key.lower()
        if not vals:
            continue
        if k == "comment":
            id3.add(COMM(encoding=3, lang="eng", desc="", text=vals))
            continue
        if k in ("lyrics", "lyric", "unsyncedlyrics"):
            # Synced (.lrc) or plain lyrics embed as USLT so players show it;
            # drop stale frames first (like APIC handling below).
            id3.delall("USLT")
            for v in vals:
                id3.add(USLT(encoding=3, lang="eng", desc="", text=v))
            continue
        frame_id = _ID3_FRAMES.get(k)
        if frame_id in frame_classes:
            id3.add(frame_classes[frame_id](encoding=3, text=vals))
        else:
            id3.add(TXXX(encoding=3, desc=k.upper(), text=vals))

    if cover:
        data, mime = cover
        # Drop any stale picture (there shouldn't be one — ffmpeg stripped
        # everything — but be safe) and embed the processed bytes as-is.
        id3.delall("APIC")
        id3.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
    audio.save()
