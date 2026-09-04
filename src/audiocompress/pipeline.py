"""Single-file + batch orchestration (pure stdlib path handling)."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import cover as cover_mod
from . import extract as extract_mod
from . import remux as remux_mod
from .metadata_map import filter_tags
from .probe import probe
from .transcode import transcode

INPUT_SUFFIXES = {
    ".flac",
    ".wav",
    ".wave",
    ".m4a",
    ".alac",
    ".mp3",
    ".ogg",
    ".oga",
    ".opus",
    ".wma",
    ".aiff",
    ".aif",
}


@dataclass
class JobConfig:
    fmt: str = "opus"  # opus | ogg
    bitrate_kbps: int = 160
    cover_max_edge: int = 1000  # 0 = keep original
    cover_quality: int = 93
    keep_all: bool = False
    keep: list[str] | None = None
    drop: list[str] | None = None


@dataclass
class JobResult:
    src: Path
    dst: Path
    in_bytes: int
    out_bytes: int
    cover_changed: bool
    cover_note: str


def compress_one(src: str | Path, dst: str | Path, cfg: JobConfig) -> JobResult:
    src, dst = Path(src), Path(dst)
    if not src.exists():
        raise FileNotFoundError(src)

    tags_raw, cover_raw = extract_mod.extract_all(src)
    tags = filter_tags(tags_raw, keep_all=cfg.keep_all, keep=cfg.keep, drop=cfg.drop)

    cover_out: tuple[bytes, str] | None = None
    cover_changed = False
    cover_note = "no cover"
    if cover_raw:
        # extract_cover_bytes returns (mime, data)
        mime0, data0 = cover_raw[0], cover_raw[1]
        new_data, new_mime, changed = cover_mod.process_cover(
            data0, max_edge=cfg.cover_max_edge, quality=cfg.cover_quality
        )
        cover_out = (new_data, new_mime)
        cover_changed = changed
        cover_note = (
            f"{mime0} {len(data0)//1024}KB -> {new_mime} {len(new_data)//1024}KB"
            + (" (resized)" if changed else " (passthrough)")
        )

    with tempfile.TemporaryDirectory(prefix="audiocompress-") as td:
        tmp = Path(td) / f"audio.{cfg.fmt}"
        transcode(src, tmp, cfg.fmt, cfg.bitrate_kbps)
        remux_mod.write_tags_cover(tmp, tags, cover_out)
        dst.parent.mkdir(parents=True, exist_ok=True)
        # atomic-ish replace, cross-platform
        tmp.replace(dst)

    return JobResult(
        src=src,
        dst=dst,
        in_bytes=src.stat().st_size,
        out_bytes=dst.stat().st_size,
        cover_changed=cover_changed,
        cover_note=cover_note,
    )


def iter_inputs(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    return sorted(
        p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in INPUT_SUFFIXES
    )


def compress_batch(
    src_root: str | Path, dst_root: str | Path, cfg: JobConfig, overwrite: bool = False
) -> list[JobResult]:
    src_root, dst_root = Path(src_root), Path(dst_root)
    results: list[JobResult] = []
    for src in iter_inputs(src_root):
        rel = src.relative_to(src_root) if src_root.is_dir() else Path(src.name)
        dst = (dst_root / rel).with_suffix(f".{cfg.fmt}")
        if dst.exists() and not overwrite:
            continue
        results.append(compress_one(src, dst, cfg))
    return results


__all__ = ["JobConfig", "JobResult", "compress_one", "compress_batch", "probe"]
