"""Cover-art processing with Pillow (never via ffmpeg re-encode)."""

from __future__ import annotations

import io

from PIL import Image

# Presets = max long-edge in px. 0 means "keep original bytes untouched".
COVER_PRESETS = (1000, 800, 600, 500)
ORIGINAL = 0


def probe_image(data: bytes) -> tuple[str, int, int]:
    with Image.open(io.BytesIO(data)) as im:
        fmt = (im.format or "JPEG").upper()
        return fmt, im.width, im.height


def guess_mime(fmt: str, data: bytes) -> str:
    f = fmt.upper()
    if f in ("JPEG", "JPG", "MPO"):
        return "image/jpeg"
    if f == "PNG":
        return "image/png"
    if f == "WEBP":
        return "image/webp"
    if f == "GIF":
        return "image/gif"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return "image/jpeg"


def process_cover(
    data: bytes, max_edge: int = 1000, quality: int = 93
) -> tuple[bytes, str, bool]:
    """Return (bytes, mime, changed).

    Passthrough when max_edge==0 or image already fits. Otherwise Lanczos
    downscale, JPEG q93 (or PNG optimize when source was PNG).
    """
    if not data:
        raise ValueError("empty cover data")
    if max_edge == ORIGINAL:
        fmt, _, _ = probe_image(data)
        return data, guess_mime(fmt, data), False

    with Image.open(io.BytesIO(data)) as im:
        fmt = (im.format or "JPEG").upper()
        w, h = im.width, im.height
        if max(w, h) <= max_edge:
            return data, guess_mime(fmt, data), False
        scale = max_edge / max(w, h)
        nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
        im = im.convert("RGB") if fmt in ("JPEG", "JPG") else im
        # JPEG sources -> RGB; PNG with alpha keeps RGBA until save decision
        resized = im.resize((nw, nh), Image.LANCZOS)
        buf = io.BytesIO()
        if fmt == "PNG":
            # Keep PNG only if small-ish; here re-save optimized PNG.
            # If it has no alpha, JPEG is smaller — but stay PNG to avoid
            # surprise format changes unless caller converts.
            save_img = resized.convert("RGBA") if "A" in resized.getbands() else resized
            save_img.save(buf, format="PNG", optimize=True)
            return buf.getvalue(), "image/png", True
        rgb = resized.convert("RGB")
        rgb.save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue(), "image/jpeg", True
