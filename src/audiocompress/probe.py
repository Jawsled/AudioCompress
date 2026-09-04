"""Read-only probing: ffprobe streams + mutagen tags/cover info."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .ffmpeg_bin import find_ffmpeg, find_ffprobe


@dataclass
class ProbeResult:
    path: Path
    duration: float | None = None
    audio_codec: str | None = None
    sample_rate: int | None = None
    channels: int | None = None
    bit_rate: int | None = None
    tags: dict = field(default_factory=dict)
    has_cover: bool = False
    cover_width: int | None = None
    cover_height: int | None = None
    cover_bytes: int = 0
    file_bytes: int = 0


def _ffprobe_json(path: Path) -> dict:
    ffprobe = find_ffprobe()
    if ffprobe:
        cmd = [
            ffprobe,
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ]
    else:  # pragma: no cover - fallback when only imageio-ffmpeg exists
        cmd = [find_ffmpeg(), "-hide_banner", "-i", str(path)]
    try:
        if ffprobe:
            out = subprocess.run(cmd, capture_output=True, text=True, check=True)
            return json.loads(out.stdout or "{}")
        return {}
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return {}


def probe(path: str | Path) -> ProbeResult:
    from mutagen import File as MutagenFile

    p = Path(path)
    info = _ffprobe_json(p)
    res = ProbeResult(path=p, file_bytes=p.stat().st_size if p.exists() else 0)

    for s in info.get("streams", []):
        if s.get("codec_type") == "audio" and res.audio_codec is None:
            res.audio_codec = s.get("codec_name")
            res.sample_rate = int(s["sample_rate"]) if s.get("sample_rate") else None
            res.channels = s.get("channels")
            if s.get("bit_rate"):
                try:
                    res.bit_rate = int(s["bit_rate"])
                except ValueError:
                    pass
        if s.get("codec_type") == "video" and s.get("disposition", {}).get("attached_pic"):
            res.has_cover = True
            res.cover_width = s.get("width")
            res.cover_height = s.get("height")

    fmt = info.get("format", {})
    if fmt.get("duration"):
        try:
            res.duration = float(fmt["duration"])
        except ValueError:
            pass
    if fmt.get("bit_rate"):
        try:
            res.bit_rate = res.bit_rate or int(fmt["bit_rate"])
        except ValueError:
            pass

    # Mutagen gives authoritative tag + embedded cover view.
    try:
        mf = MutagenFile(p)
        if mf is not None:
            res.tags = dict(mf.tags) if getattr(mf, "tags", None) else {}
            from .extract import extract_cover_bytes

            cover = extract_cover_bytes(mf)
            if cover:
                from .cover import probe_image

                res.has_cover = True
                res.cover_bytes = len(cover[1])
                try:
                    _, w, h = probe_image(cover[1])
                    res.cover_width, res.cover_height = w, h
                except Exception:
                    pass
    except Exception:
        pass
    return res
