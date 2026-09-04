"""Extract tags + cover bytes via mutagen (no re-encode, cross-platform)."""

from __future__ import annotations

import base64
from pathlib import Path


def extract_cover_bytes(mf) -> tuple[str, bytes] | None:
    """Return (mime, data) or None. Handles FLAC/MP3/MP4/Ogg/Opus/WavPack-ish."""
    # FLAC / WavPack pictures
    pics = getattr(mf, "pictures", None)
    if pics:
        p = pics[0]
        return p.mime or "image/jpeg", bytes(p.data)
    tags = getattr(mf, "tags", None)
    if not tags:
        return None
    # MP3 APIC
    for key in list(tags.keys()):
        if key.startswith("APIC"):
            apic = tags[key]
            return apic.mime or "image/jpeg", bytes(apic.data)
    # MP4 covr
    if "covr" in tags:
        covrs = tags["covr"]
        if covrs:
            from mutagen.mp4 import MP4Cover

            c = covrs[0]
            mime = (
                "image/png"
                if getattr(c, "imageformat", None) == MP4Cover.FORMAT_PNG
                else "image/jpeg"
            )
            return mime, bytes(c)
    # Vorbis comments: METADATA_BLOCK_PICTURE (Opus/Ogg/FLAC-via-comments)
    for key in ("metadata_block_picture", "METADATA_BLOCK_PICTURE"):
        if key in tags:
            try:
                from mutagen.flac import Picture

                raw = base64.b64decode(str(tags[key][0]))
                pic = Picture()
                import io

                pic.load(io.BytesIO(raw))
                return pic.mime or "image/jpeg", bytes(pic.data)
            except Exception:
                continue
    return None


def read_tags(mf) -> dict[str, list[str]]:
    """Flatten mutagen tags to {lower_key: [str values]} (cover keys excluded)."""
    out: dict[str, list[str]] = {}
    tags = getattr(mf, "tags", None)
    if not tags:
        return out
    # MP4 atoms are non-string keys sometimes; normalize carefully
    from mutagen.mp4 import MP4Tags

    MP4_MAP = {
        "\xa9nam": "title",
        "\xa9ART": "artist",
        "\xa9alb": "album",
        "\xa9gen": "genre",
        "\xa9day": "date",
        "\xa9wrt": "composer",
        "aART": "albumartist",
        "\xa9cmt": "comment",
        "trkn": "tracknumber",
        "disk": "discnumber",
    }
    if isinstance(tags, MP4Tags):
        for k, v in tags.items():
            name = MP4_MAP.get(k, k.strip().lower() if isinstance(k, str) else str(k))
            if k == "covr":
                continue
            if k in ("trkn", "disk"):
                try:
                    out[name] = [str(v[0][0])]
                    continue
                except Exception:
                    pass
            out[name] = [str(x) for x in (v if isinstance(v, list) else [v])]
        return out

    # MP3 ID3 frames (TIT2, TPE1, ...) -> normalized names; TXXX:FOO -> foo.
    try:
        from mutagen.id3 import ID3

        is_id3 = isinstance(tags, ID3)
    except ImportError:  # pragma: no cover
        is_id3 = False
    if is_id3:
        ID3_MAP = {
            "TIT2": "title",
            "TPE1": "artist",
            "TALB": "album",
            "TPE2": "albumartist",
            "TCOM": "composer",
            "TCON": "genre",
            "TDRC": "date",
            "TYER": "date",
            "TRCK": "tracknumber",
            "TPOS": "discnumber",
            "COMM": "comment",
            "TIT3": "description",
            "TEXT": "lyricist",
            "TPE3": "conductor",
            "TPUB": "label",
        }
        for k, v in tags.items():
            if k.startswith("APIC"):
                continue
            if k.startswith("TXXX:"):
                name = k.split(":", 1)[1].strip().lower()
            elif k.startswith("COMM"):
                name = "comment"
            else:
                name = ID3_MAP.get(k.split(":")[0], k.strip().lower())
            try:
                vals = v.text if hasattr(v, "text") else v
                vals = vals if isinstance(vals, list) else [vals]
                strs = [str(x) for x in vals if str(x)]
            except Exception:
                continue
            if strs:
                out.setdefault(name, []).extend(s for s in strs if s not in out.get(name, []))
        return out

    for k, v in tags.items():
        lk = str(k).lower()
        if lk.startswith("apic") or lk in ("covr", "metadata_block_picture"):
            continue
        if lk.startswith("cover") or "picture" in lk:
            continue
        vals = v if isinstance(v, list) else [v]
        strs: list[str] = []
        for x in vals:
            # mutagen FLAC picture objects etc. — skip non-text
            if hasattr(x, "data") and hasattr(x, "mime"):
                continue
            strs.append(str(x))
        if strs:
            # Vorbis keys may look like 'title\x00...' — keep simple
            out[lk.split("\x00")[0]] = strs
    return out


def extract_all(path: str | Path) -> tuple[dict[str, list[str]], tuple[str, bytes] | None]:
    from mutagen import File as MutagenFile

    mf = MutagenFile(path)
    if mf is None:
        raise ValueError(f"unsupported audio file: {path}")
    return read_tags(mf), extract_cover_bytes(mf)


def collect_tag_summary(
    paths, sample_files: int = 50, sample_values: int = 3
) -> dict[str, list[str]]:
    """Scan files, return {tag_key: [example values]} for tag discovery UI.

    Only the first `sample_files` files are read, so huge libraries stay fast.
    """
    from mutagen import File as MutagenFile

    summary: dict[str, list[str]] = {}
    counts: dict[str, int] = {}
    for i, p in enumerate(paths):
        if i >= sample_files:
            break
        try:
            mf = MutagenFile(p)
            if mf is None:
                continue
            for k, vals in read_tags(mf).items():
                counts[k] = counts.get(k, 0) + 1
                for v in vals:
                    if v and v not in summary.setdefault(k, []) and len(summary[k]) < sample_values:
                        summary[k].append(v)
        except Exception:
            continue
    # Most common tags first.
    return dict(sorted(summary.items(), key=lambda kv: -counts.get(kv[0], 0)))
