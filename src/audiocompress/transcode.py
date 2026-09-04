"""Audio-only transcode via ffmpeg. Strips metadata/video so cover can't degrade."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .ffmpeg_bin import find_ffmpeg

SUPPORTED_OUT = ("opus", "ogg", "mp3")

# MP3 uses plain CBR (-b:a only) while Opus/Vorbis get constrained VBR on top.
_CODECS = {"opus": "libopus", "ogg": "libvorbis", "mp3": "libmp3lame"}


def build_command(
    src: Path, dst: Path, fmt: str, bitrate_kbps: int
) -> list[str]:
    ffmpeg = find_ffmpeg()
    fmt = fmt.lower().lstrip(".")
    if fmt not in SUPPORTED_OUT:
        raise ValueError(f"unsupported output format {fmt!r} (use opus/ogg/mp3)")
    if not 32 <= bitrate_kbps <= 320:
        raise ValueError(f"bitrate {bitrate_kbps} out of range (32-320 kbps)")
    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(src),
        "-map",
        "0:a:0",  # first audio stream only — never the attached picture
        "-vn",  # belt & braces: no video/cover stream
        "-map_metadata",
        "-1",  # strip all tags here; re-embed later via mutagen
        "-c:a",
        _CODECS[fmt],
        "-b:a",
        f"{bitrate_kbps}k",
    ]
    if fmt in ("opus", "ogg"):
        cmd += ["-vbr", "on"]
    if fmt == "opus":
        cmd += ["-application", "audio"]
    return cmd + [str(dst)]


def transcode(src: str | Path, dst: str | Path, fmt: str, bitrate_kbps: int) -> Path:
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = build_command(src, dst, fmt, bitrate_kbps)
    subprocess.run(cmd, check=True)
    return dst
