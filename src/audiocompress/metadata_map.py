"""Normalized metadata handling across FLAC/MP3/MP4/Vorbis/Opus."""

from __future__ import annotations

from pathlib import Path

# Whitelist kept by default: saves space, avoids Opus-incompatible blobs.
DEFAULT_KEEP = [
    "title",
    "artist",
    "album",
    "albumartist",
    "displayartist",
    "displayalbumartist",
    "composer",
    "performer",
    "genre",
    "date",
    "tracknumber",
    "discnumber",
    "label",
    "comment",
    "description",
]

# Human-friendly names shown in the tag picker / `tags` command so users
# don't need to know raw tag keys.
FRIENDLY_TAGS: dict[str, tuple[str, str]] = {
    "title": ("Title", "Track name"),
    "artist": ("Artist", "Track artist"),
    "album": ("Album", "Album name"),
    "albumartist": ("Album artist", "Main artist of the whole album"),
    "displayartist": ("Display artist", "Artist name as the player should show it"),
    "displayalbumartist": ("Display album artist", "Album artist as the player should show it"),
    "composer": ("Composer", "Who wrote the music"),
    "performer": ("Performer", "Who performed it"),
    "conductor": ("Conductor", "Orchestra conductor"),
    "lyricist": ("Lyricist", "Who wrote the lyrics"),
    "genre": ("Genre", "e.g. Rock, Jazz, Classical"),
    "date": ("Release date", "Year or full release date"),
    "tracknumber": ("Track number", "Track position on the disc"),
    "discnumber": ("Disc number", "Disc position in multi-disc releases"),
    "label": ("Label", "Record label"),
    "catalognumber": ("Catalog number", "Label's release identifier"),
    "isrc": ("ISRC", "International recording code"),
    "barcode": ("Barcode", "Release barcode / UPC"),
    "comment": ("Comment", "Free-text comment"),
    "description": ("Description", "Release/track description"),
    "lyrics": ("Lyrics", "Embedded song lyrics"),
    "bpm": ("BPM", "Beats per minute"),
    "key": ("Key", "Musical key"),
    "mood": ("Mood", "Mood tag"),
}


def friendly_label(key: str) -> tuple[str, str]:
    """Return (Label, description); unknown tags get a title-cased fallback."""
    if key in FRIENDLY_TAGS:
        return FRIENDLY_TAGS[key]
    return key.replace("_", " ").title(), "Custom tag"


# Container-specific native names for normalized keys, so the tag picker can
# show e.g. "TIT2 (ID3)" or "TITLE (Vorbis)" next to the friendly label.
NATIVE_KEYS: dict[str, dict[str, str]] = {
    "ID3": {
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
    },
    "FLAC": {
        "title": "TITLE",
        "artist": "ARTIST",
        "album": "ALBUM",
        "albumartist": "ALBUMARTIST",
        "composer": "COMPOSER",
        "genre": "GENRE",
        "date": "DATE",
        "tracknumber": "TRACKNUMBER",
        "discnumber": "DISCNUMBER",
        "lyricist": "LYRICIST",
        "label": "LABEL",
    },
    "Vorbis": {
        "title": "TITLE",
        "artist": "ARTIST",
        "album": "ALBUM",
        "albumartist": "ALBUMARTIST",
        "composer": "COMPOSER",
        "genre": "GENRE",
        "date": "DATE",
        "tracknumber": "TRACKNUMBER",
        "discnumber": "DISCNUMBER",
    },
    "MP4": {
        "title": "\u00a9nam",
        "artist": "\u00a9ART",
        "album": "\u00a9alb",
        "albumartist": "aART",
        "composer": "\u00a9wrt",
        "genre": "\u00a9gen",
        "date": "\u00a9day",
        "tracknumber": "trkn",
        "discnumber": "disk",
    },
}


def native_key(key: str, fmt: str) -> str | None:
    """Return the native frame/atom name for `key` in container `fmt`."""
    return NATIVE_KEYS.get(fmt, {}).get(key.lower())


def detect_format(path) -> str:
    """Best-effort container name for a single file: 'ID3'/'FLAC'/'Vorbis'/'MP4'."""
    from mutagen import File as MutagenFile

    p = Path(path)
    try:
        mf = MutagenFile(p)
    except Exception:
        return "Other"
    if mf is None or mf.tags is None:
        # FLAC/Opus/Vorbis without tags: still try the class name.
        return _format_from_class(mf)
    cls = type(mf).__name__
    if cls == "FLAC":
        return "FLAC"
    if cls in ("OggOpus", "OggVorbis"):
        return "Vorbis"
    if cls == "MP3":
        return "ID3"
    if cls == "MP4":
        return "MP4"
    return _format_from_class(mf)


def _format_from_class(mf) -> str:
    cls = type(mf).__name__ if mf is not None else ""
    if cls == "FLAC":
        return "FLAC"
    if cls in ("OggOpus", "OggVorbis"):
        return "Vorbis"
    if cls == "MP3":
        return "ID3"
    if cls == "MP4":
        return "MP4"
    return "Other"


def detect_batch_format(paths) -> dict[str, str]:
    """Map {normalized_tag_key: container_label} for a batch (multi-format
    libraries use 'Mixed')."""
    if not paths:
        return {}
    fmts: set[str] = set()
    key_to_fmts: dict[str, set[str]] = {}
    from .extract import read_tags

    for p in paths:
        fmt = detect_format(p)
        fmts.add(fmt)
        from mutagen import File as MutagenFile

        try:
            mf = MutagenFile(p)
        except Exception:
            continue
        if mf is None:
            continue
        for k in read_tags(mf):
            key_to_fmts.setdefault(k, set()).add(fmt)
    overall = "Mixed" if len(fmts) > 1 else (next(iter(fmts)) if fmts else "Other")
    out: dict[str, str] = {k: overall for k in key_to_fmts}
    if len(fmts) > 1:
        for k, s in key_to_fmts.items():
            if len(s) == 1:
                out[k] = next(iter(s))
    return out

# Tags always dropped (size / compatibility reasons).
ALWAYS_DROP = {
    "encoder",
    "encodedby",
    "cuesheet",
    "replaygain_track_gain",
    "replaygain_album_gain",
    "replaygain_track_peak",
    "replaygain_album_peak",
}


def normalize_tags(raw: dict[str, list[str]]) -> dict[str, list[str]]:
    """Lowercase keys, keep list-of-str values."""
    out: dict[str, list[str]] = {}
    for k, v in raw.items():
        key = k.strip().lower()
        vals = v if isinstance(v, list) else [v]
        out[key] = [str(x) for x in vals]
    return out


def filter_tags(
    tags: dict[str, list[str]],
    keep_all: bool = False,
    keep: list[str] | None = None,
    drop: list[str] | None = None,
) -> dict[str, list[str]]:
    tags = normalize_tags(tags)
    drop_set = set(ALWAYS_DROP)
    if drop:
        drop_set.update(d.strip().lower() for d in drop)
    if keep_all:
        wanted: set[str] | None = None
    elif keep:
        wanted = {k.strip().lower() for k in keep}
    else:
        wanted = set(DEFAULT_KEEP)
    out: dict[str, list[str]] = {}
    for k, v in tags.items():
        if k in drop_set:
            continue
        if wanted is not None and k not in wanted:
            continue
        out[k] = v
    return out
