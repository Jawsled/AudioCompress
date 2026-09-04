"""Cross-platform ffmpeg/ffprobe resolution.

Prefers system binaries, falls back to imageio-ffmpeg static binary
(ships Windows/macOS/Linux builds, no platform-specific code needed).
"""

from __future__ import annotations

import shutil


def find_ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if path:
        return path
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # pragma: no cover
        raise FileNotFoundError(
            "ffmpeg not found on PATH and imageio-ffmpeg fallback failed. "
            "Install ffmpeg or `pip install imageio-ffmpeg`."
        ) from exc


def find_ffprobe() -> str | None:
    """ffprobe has no imageio fallback; may be None (probe then uses ffmpeg)."""
    return shutil.which("ffprobe")
