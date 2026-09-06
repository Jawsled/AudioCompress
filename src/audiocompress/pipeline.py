"""Single-file + batch orchestration (pure stdlib path handling)."""

from __future__ import annotations

import locale
import os
import shutil
import tempfile
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from . import cover as cover_mod
from . import extract as extract_mod
from . import remux as remux_mod
from .metadata_map import filter_tags
from .probe import probe
from .transcode import transcode_captured

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

# Companion files (lyrics, notes, cuesheets-as-text) mirrored verbatim.
SIDECAR_SUFFIXES = {".lrc", ".txt"}

# Final transcoded outputs. Used for duplicate prevention: a source stem is
# considered "already exported" if any of these exists in the destination
# folder, regardless of which one the current run would write.
OUTPUT_SUFFIXES = {".opus", ".ogg", ".mp3"}


@dataclass
class JobConfig:
    fmt: str = "opus"  # opus | ogg
    bitrate_kbps: int = 160
    cover_max_edge: int = 1000  # 0 = keep original
    cover_quality: int = 93
    keep_all: bool = False
    keep: list[str] | None = None
    drop: list[str] | None = None
    embed_lrc: bool = False  # read same-stem .lrc -> lyrics tag
    embed_txt: bool = False  # read same-stem .txt -> comment tag


def default_jobs(requested: int | None = None) -> int:
    """Resolve worker count: explicit > settings > cpu count (capped at 8)."""
    if requested and requested > 0:
        return min(requested, 32)
    try:
        n = os.cpu_count() or 4
    except NotImplementedError:
        n = 4
    return max(1, min(n, 8))


@dataclass
class JobResult:
    src: Path
    dst: Path
    in_bytes: int
    out_bytes: int
    cover_changed: bool
    cover_note: str
    warnings: str = ""  # ffmpeg decode warnings (corrupt frames concealed, …)
    sidecar_note: str = ""  # e.g. "embedded song.lrc->lyrics"


# -- sidecar embedding ----------------------------------------------------

# Cap embedded text so a stray 10MB booklet .txt can't blow up every tag.
MAX_EMBED_CHARS = 100_000


def _norm_stem(name: str) -> str:
    """Canonical stem for same-name comparisons.

    NFC + casefold so matches survive Unicode normalization differences
    (macOS writes NFD `u + combining diaeresis`, Windows/most tools use NFC
    `ü` — same album, different bytes) and case differences. Without this,
    `04. Pür Love.opus` (NFC) doesn't block `04. Pür Love.opus` (NFD) and
    a look-alike duplicate gets created.
    """
    return unicodedata.normalize("NFC", name.casefold())


def companion_for(src_file: str | Path, suffix: str) -> Path | None:
    """Same-stem sibling (e.g. song.flac -> song.lrc), or None."""
    src_file = Path(src_file)
    stem = _norm_stem(src_file.stem)
    suffix = suffix.lower()
    try:
        siblings = list(src_file.parent.iterdir())
    except OSError:
        return None
    for p in siblings:
        if p.is_file() and p.suffix.lower() == suffix and _norm_stem(p.stem) == stem:
            return p
    return None


def read_sidecar_text(path: str | Path) -> str:
    """Read .lrc/.txt with tolerant decoding; normalized \\n newlines."""
    raw = Path(path).read_bytes()
    text: str | None = None
    for enc in ("utf-8-sig", "utf-8", locale.getpreferredencoding(False), "cp1252"):
        try:
            text = raw.decode(enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if text is None:
        text = raw.decode("utf-8", errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) > MAX_EMBED_CHARS:
        text = text[:MAX_EMBED_CHARS].rstrip() + "\n…[truncated]"
    return text


def collect_embed_tags(
    src: str | Path, cfg: JobConfig
) -> tuple[dict[str, list[str]], str]:
    """Read same-stem .lrc/.txt per cfg flags. Returns (tags, note)."""
    extra: dict[str, list[str]] = {}
    notes: list[str] = []
    src_p = Path(src)
    if cfg.embed_lrc:
        lrc = companion_for(src_p, ".lrc")
        if lrc is not None:
            try:
                text = read_sidecar_text(lrc)
                if text:
                    extra["lyrics"] = [text]
                    notes.append(f"{lrc.name}->lyrics")
            except OSError as exc:
                notes.append(f"{lrc.name} unreadable ({exc})")
    if cfg.embed_txt:
        txt = companion_for(src_p, ".txt")
        if txt is not None:
            try:
                text = read_sidecar_text(txt)
                if text:
                    # Append to any existing comment so source notes survive.
                    extra["comment"] = [*extra.get("comment", []), text]
                    notes.append(f"{txt.name}->comment")
            except OSError as exc:
                notes.append(f"{txt.name} unreadable ({exc})")
    return extra, "; ".join(notes)


def compress_one(
    src: str | Path,
    dst: str | Path,
    cfg: JobConfig,
    copy_sidecars: bool = True,
    overwrite_sidecars: bool = False,
) -> JobResult:
    src, dst = Path(src), Path(dst)
    if not src.exists():
        raise FileNotFoundError(src)

    tags_raw, cover_raw = extract_mod.extract_all(src)
    tags = filter_tags(tags_raw, keep_all=cfg.keep_all, keep=cfg.keep, drop=cfg.drop)

    # Sidecar embedding wins over --keep/--drop: explicit checkbox.
    sidecar_note = ""
    if cfg.embed_lrc or cfg.embed_txt:
        extra, sidecar_note = collect_embed_tags(src, cfg)
        for k, vals in extra.items():
            if k == "comment" and k in tags:
                tags[k] = [*tags[k], *[v for v in vals if v not in tags[k]]]
            else:
                tags[k] = vals

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
        _, audio_warning = transcode_captured(src, tmp, cfg.fmt, cfg.bitrate_kbps)
        remux_mod.write_tags_cover(tmp, tags, cover_out)
        dst.parent.mkdir(parents=True, exist_ok=True)
        # atomic-ish replace, cross-platform
        tmp.replace(dst)

    if copy_sidecars:
        skip: set[str] = set()
        # "embed instead of copy": don't leave a duplicate file behind.
        if cfg.embed_lrc:
            skip.add(".lrc")
        if cfg.embed_txt:
            skip.add(".txt")
        copy_sidecars_for_file(src, dst, overwrite=overwrite_sidecars, skip_suffixes=skip)

    return JobResult(
        src=src,
        dst=dst,
        in_bytes=src.stat().st_size,
        out_bytes=dst.stat().st_size,
        cover_changed=cover_changed,
        cover_note=cover_note,
        warnings=audio_warning,
        sidecar_note=sidecar_note,
    )


def iter_inputs(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    return sorted(
        p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in INPUT_SUFFIXES
    )


def iter_sidecars(root: Path) -> list[Path]:
    """List companion .lrc/.txt files.

    Directory root: every .lrc/.txt under the tree (recursive).
    File root: siblings sharing the same stem (e.g. song.flac -> song.lrc).
    """
    root = Path(root)
    if root.is_file():
        stem = _norm_stem(root.stem)
        try:
            siblings = list(root.parent.iterdir())
        except OSError:
            return []
        return sorted(
            p
            for p in siblings
            if p.is_file()
            and p.suffix.lower() in SIDECAR_SUFFIXES
            and _norm_stem(p.stem) == stem
        )
    return sorted(
        p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in SIDECAR_SUFFIXES
    )


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


def existing_output(dst: str | Path) -> Path | None:
    """Return the already-exported file blocking `dst`, or None.

    Duplicate prevention, ignoring extensions: `dst` is considered done if
    the exact path exists OR a same-stem sibling with any supported output
    suffix (opus/ogg/mp3) exists in the same folder. Stem comparison is
    NFC-normalized + case-insensitive, so `Song.OPUS` blocks `song.opus`
    and NFC `Pür` blocks NFD `Pür` (macOS vs Windows spellings of ü).

    Only OUTPUT_SUFFIXES count — same-stem `.lrc`/`.txt` sidecars never
    block a transcode.
    """
    dst = Path(dst)
    if dst.exists():
        return dst
    parent = dst.parent
    try:
        if not parent.is_dir():
            return None
    except OSError:
        return None
    stem = _norm_stem(dst.stem)
    try:
        entries = list(parent.iterdir())
    except OSError:
        return None
    for p in entries:
        try:
            if not p.is_file():
                continue
        except OSError:
            continue
        if p.suffix.lower() in OUTPUT_SUFFIXES and _norm_stem(p.stem) == stem:
            return p
    return None


def output_exists(dst: str | Path) -> bool:
    """True if `dst` or any same-stem output already exists (see existing_output)."""
    return existing_output(dst) is not None


def copy_sidecars_for_file(
    src_file: str | Path,
    dst_file: str | Path,
    overwrite: bool = False,
    skip_suffixes: set[str] | None = None,
) -> list[Path]:
    """Copy same-stem .lrc/.txt siblings next to the transcoded output."""
    src_file, dst_file = Path(src_file), Path(dst_file)
    skip = {s.lower() for s in (skip_suffixes or set())}
    copied: list[Path] = []
    for sib in iter_sidecars(src_file):
        if sib.suffix.lower() in skip:
            continue
        if _same_file(sib, dst_file):
            continue
        target = dst_file.parent / sib.name
        if _same_file(sib, target):
            continue
        if target.exists() and not overwrite:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sib, target)
        copied.append(target)
    return copied


def copy_sidecars(
    src_root: str | Path,
    dst_root: str | Path,
    overwrite: bool = False,
    skip_suffixes: set[str] | None = None,
) -> list[tuple[Path, Path]]:
    """Mirror every .lrc/.txt under src_root into dst_root, preserving tree.

    Single-file src_root degrades to same-stem siblings (see
    copy_sidecars_for_file); dst_root is treated as the output directory
    in that case.
    """
    src_root, dst_root = Path(src_root), Path(dst_root)
    skip = {s.lower() for s in (skip_suffixes or set())}
    if not src_root.exists():
        return []
    if src_root.is_file():
        dst_dir = dst_root if dst_root.suffix.lower() not in INPUT_SUFFIXES | {".opus", ".ogg", ".mp3"} else dst_root.parent
        # dst_dir heuristic above is best-effort; fall back to parent handling
        # inside copy_sidecars_for_file when dst_root is an audio file path.
        if dst_root.is_dir() or not dst_root.suffix:
            dst_dir = dst_root
            copied = []
            for sib in iter_sidecars(src_root):
                if sib.suffix.lower() in skip:
                    continue
                target = dst_dir / sib.name
                if _same_file(sib, target):
                    continue
                if target.exists() and not overwrite:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(sib, target)
                copied.append((sib, target))
            return copied
        # dst_root looks like a file path -> copy next to it
        targets = copy_sidecars_for_file(
            src_root, dst_root, overwrite=overwrite, skip_suffixes=skip
        )
        return [(src_root.parent / t.name, t) for t in targets]
    copied: list[tuple[Path, Path]] = []
    for src in iter_sidecars(src_root):
        if src.suffix.lower() in skip:
            continue
        rel = src.relative_to(src_root)
        dst = dst_root / rel
        if _same_file(src, dst):
            continue
        if dst.exists() and not overwrite:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied.append((src, dst))
    return copied


def _skip_for_cfg(cfg: JobConfig) -> set[str]:
    skip: set[str] = set()
    if cfg.embed_lrc:
        skip.add(".lrc")
    if cfg.embed_txt:
        skip.add(".txt")
    return skip


def _compress_one_job(
    src: Path, dst: Path, cfg: JobConfig, overwrite: bool
) -> JobResult | None:
    """Single job for the pool; None means skipped (output exists)."""
    if not overwrite and output_exists(dst):
        return None
    return compress_one(src, dst, cfg, overwrite_sidecars=overwrite)


def compress_batch(
    src_root: str | Path,
    dst_root: str | Path,
    cfg: JobConfig,
    overwrite: bool = False,
    with_sidecars: bool = True,
    jobs: int | None = None,
    progress_cb=None,
) -> list[JobResult]:
    """Batch transcode, parallel across files (thread pool, ffmpeg releases GIL).

    `jobs`: worker count (None/0 = auto = cpu count capped at 8).
    `progress_cb(done, total)` is called thread-safely as each file finishes.
    """
    src_root, dst_root = Path(src_root), Path(dst_root)
    pairs = [
        (src, (dst_root / src.relative_to(src_root)).with_suffix(f".{cfg.fmt}")
         if src_root.is_dir() else (dst_root / src.name).with_suffix(f".{cfg.fmt}"))
        for src in iter_inputs(src_root)
    ]
    if overwrite:
        todo = pairs
    else:
        # Stem-insensitive: skip if dst or any same-stem .opus/.ogg/.mp3 exists.
        todo = [(s, d) for s, d in pairs if not output_exists(d)]
    results: list[JobResult] = []
    n_workers = default_jobs(jobs)
    if not todo:
        if with_sidecars:
            copy_sidecars_batch(
                src_root, dst_root, overwrite=overwrite, skip_suffixes=_skip_for_cfg(cfg)
            )
        return results
    if n_workers <= 1 or len(todo) == 1:
        for src, dst in todo:
            results.append(compress_one(src, dst, cfg, overwrite_sidecars=overwrite))
            if progress_cb is not None:
                progress_cb(len(results), len(todo))
    else:
        results_by_src: dict[Path, JobResult] = {}
        done = 0
        with ThreadPoolExecutor(max_workers=n_workers) as pool:
            futs = {
                pool.submit(_compress_one_job, src, dst, cfg, overwrite): (src, dst)
                for src, dst in todo
            }
            for fut in as_completed(futs):
                res = fut.result()  # per-file errors propagate to caller
                if res is not None:
                    results_by_src[res.src] = res
                done += 1
                if progress_cb is not None:
                    progress_cb(done, len(todo))
        # Deterministic order for tables/logs regardless of finish order.
        order = {s: i for i, (s, _) in enumerate(todo)}
        results = sorted(results_by_src.values(), key=lambda r: order.get(r.src, 0))
    if with_sidecars:
        # Full-tree mirror catches orphan sidecars (no matching audio stem,
        # subfolders without audio, etc.). Same-stem companions were already
        # copied per-file above; existing outputs are skipped unless overwrite.
        copy_sidecars_batch(
            src_root, dst_root, overwrite=overwrite, skip_suffixes=_skip_for_cfg(cfg)
        )
    return results


def copy_sidecars_batch(
    src_root: str | Path,
    dst_root: str | Path,
    overwrite: bool = False,
    skip_suffixes: set[str] | None = None,
) -> list[tuple[Path, Path]]:
    """Batch-tree variant used by compress_batch/CLI/GUI (dirs only)."""
    return copy_sidecars(
        src_root, dst_root, overwrite=overwrite, skip_suffixes=skip_suffixes
    )


__all__ = [
    "JobConfig",
    "JobResult",
    "MAX_EMBED_CHARS",
    "OUTPUT_SUFFIXES",
    "SIDECAR_SUFFIXES",
    "collect_embed_tags",
    "companion_for",
    "compress_one",
    "compress_batch",
    "copy_sidecars",
    "copy_sidecars_for_file",
    "copy_sidecars_batch",
    "default_jobs",
    "existing_output",
    "iter_inputs",
    "iter_sidecars",
    "output_exists",
    "probe",
    "read_sidecar_text",
]
