"""Audio-only transcode via ffmpeg. Strips metadata/video so cover can't degrade."""

from __future__ import annotations

import re
import subprocess
import warnings
from pathlib import Path

from .ffmpeg_bin import find_ffmpeg

SUPPORTED_OUT = ("opus", "ogg", "mp3")

# MP3 uses plain CBR (-b:a only) while Opus/Vorbis get constrained VBR on top.
_CODECS = {"opus": "libopus", "ogg": "libvorbis", "mp3": "libmp3lame"}

# ffmpeg exits 0 even when it conceals corrupt input frames, so scan the
# captured stderr for these to warn per-file instead of spamming the console.
_ISSUE_RE = re.compile(
    r"invalid (residual|data|subframe|stream)|decode_frame\(\) failed|"
    r"decoding error|error (decoding|submitting)|header missing|"
    r"corrupt|truncat|conceal|invalid data found when processing input",
    re.IGNORECASE,
)


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
        "-nostdin",
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


def summarize_stderr(stderr: str, max_lines: int = 3) -> str:
    """Condense ffmpeg stderr to a short warning (deduped, one line per kind)."""
    seen: list[str] = []
    for line in (stderr or "").splitlines():
        s = line.strip()
        if not s or not _ISSUE_RE.search(s):
            continue
        # Strip ffmpeg prefixes like "[flac @ 000...] " / "[aist#0:0/...] ".
        s = re.sub(r"^\[[^\]]+\]\s*", "", s)
        if s not in seen:
            seen.append(s)
        if len(seen) >= max_lines:
            break
    return "; ".join(seen)


def transcode_captured(
    src: str | Path, dst: str | Path, fmt: str, bitrate_kbps: int
) -> tuple[Path, str]:
    """Transcode, capturing ffmpeg stderr. Returns (dst, warning_summary).

    Stderr is captured so decode warnings (corrupt FLAC frames, bad MP3
    headers, broken cover EXIF) don't leak to the console — the caller
    decides how to surface the summary per file.
    """
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = build_command(src, dst, fmt, bitrate_kbps)
    # utf-8/replace: ffmpeg echoes byte-level stream info; locale decoding
    # (e.g. cp1252 on Windows) must never turn that into UnicodeDecodeError.
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if proc.returncode != 0:
        err = (proc.stderr or "").strip().splitlines()
        tail = "\n".join(err[-10:]) if err else f"exit {proc.returncode}"
        raise subprocess.CalledProcessError(
            proc.returncode, cmd, output=proc.stdout, stderr=tail
        )
    return dst, summarize_stderr(proc.stderr or "")


def transcode(src: str | Path, dst: str | Path, fmt: str, bitrate_kbps: int) -> Path:
    dst_p, warning = transcode_captured(src, dst, fmt, bitrate_kbps)
    if warning:
        warnings.warn(f"{Path(src).name}: {warning}", UserWarning, stacklevel=2)
    return dst_p
