"""Typer CLI: single file + batch folder modes."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.progress import Progress
from rich.table import Table

from . import settings
from .cover import COVER_PRESETS
from .extract import collect_tag_summary
from .metadata_map import DEFAULT_KEEP, friendly_label
from .pipeline import (
    JobConfig,
    compress_batch,
    compress_one,
    copy_sidecars,
    default_jobs,
    existing_output,
    iter_inputs,
    iter_sidecars,
    output_exists,
)
from .probe import probe

app = typer.Typer(add_completion=False, help="Compress audio, preserve cover art.")
console = Console()


def _display(s: str) -> str:
    """Keep a string printable on legacy Windows consoles (e.g. cp1252).

    UTF-8 terminals pass through untouched; when the console encoding can't
    represent a char (NFD `u + combining diaeresis` on cp1252), fall back to
    backslash escapes so `console.print` never raises UnicodeEncodeError
    mid-batch (which previously skipped sidecar copy + settings save).
    """
    enc = getattr(console.file, "encoding", None) or "utf-8"
    try:
        s.encode(enc)
        return s
    except (UnicodeEncodeError, LookupError):
        return s.encode(enc, "backslashreplace").decode(enc)

FmtOpt = typer.Option(None, "--format", "-f", help="Output format: opus, ogg or mp3 (default: saved setting or opus).")
BitrateOpt = typer.Option(None, "--bitrate", "-b", help="Audio bitrate kbps, 32-320 (default: saved setting or 160).")
CoverOpt = typer.Option(
    None, "--cover-size", help=f"Cover preset, long-edge px {sorted(COVER_PRESETS)} or 0=original (default: saved setting or 1000)."
)
QualityOpt = typer.Option(None, "--cover-quality", help="JPEG quality for resized covers (default: saved setting or 93).")
KeepAllOpt = typer.Option(False, "--keep-all", help="Keep all source tags.")
KeepOpt = typer.Option(None, "--keep", help="Comma-separated tags to keep (overrides default whitelist).")
DropOpt = typer.Option(None, "--drop", help="Comma-separated tags to drop.")
OverwriteOpt = typer.Option(False, "--overwrite/--no-overwrite", help="Overwrite existing outputs.")
DryRunOpt = typer.Option(False, "--dry-run", help="Probe only, transcode nothing.")
EmbedLrcOpt = typer.Option(None, "--embed-lrc/--no-embed-lrc", help="Embed same-stem .lrc into lyrics tag instead of copying the file.")
EmbedTxtOpt = typer.Option(None, "--embed-txt/--no-embed-txt", help="Embed same-stem .txt into comment tag instead of copying the file.")
JobsOpt = typer.Option(None, "--jobs", "-j", help="Parallel workers for batch (default: CPU count, max 8).")


def _parse_csv(v: Optional[str]) -> Optional[list[str]]:
    return [s.strip() for s in v.split(",") if s.strip()] if v else None


def _cfg(fmt, bitrate, cover_size, cover_quality, keep_all, keep, drop,
         embed_lrc=None, embed_txt=None) -> JobConfig:
    saved = settings.load()
    fmt = (fmt or saved.get("format", "opus")).lower().lstrip(".")
    if fmt not in ("opus", "ogg", "mp3"):
        raise typer.BadParameter(f"--format must be opus, ogg or mp3 (got {fmt!r})")
    bitrate = int(bitrate if bitrate is not None else saved.get("bitrate_kbps", 160))
    if not 32 <= bitrate <= 320:
        raise typer.BadParameter(f"--bitrate must be 32-320 (got {bitrate})")
    cover_size = int(cover_size if cover_size is not None else saved.get("cover_max_edge", 1000))
    if cover_size not in (0, *COVER_PRESETS):
        raise typer.BadParameter(f"--cover-size must be one of 0{sorted(COVER_PRESETS)}")
    cover_quality = int(cover_quality if cover_quality is not None else saved.get("cover_quality", 93))
    keep_list = _parse_csv(keep) if keep is not None else saved.get("keep")
    drop_list = _parse_csv(drop) if drop is not None else saved.get("drop")
    if embed_lrc is None:
        embed_lrc = bool(saved.get("embed_lrc", False))
    if embed_txt is None:
        embed_txt = bool(saved.get("embed_txt", False))
    return JobConfig(
        fmt=fmt,
        bitrate_kbps=bitrate,
        cover_max_edge=cover_size,
        cover_quality=cover_quality,
        keep_all=keep_all,
        keep=keep_list,
        drop=drop_list,
        embed_lrc=bool(embed_lrc),
        embed_txt=bool(embed_txt),
    )


def _jobs(jobs) -> int:
    saved = settings.load()
    if jobs is None:
        jobs = saved.get("jobs")
    return default_jobs(jobs)


def _remember(cfg: JobConfig, dst_dir: Path, jobs: int | None = None) -> None:
    """Persist last-used options so CLI and GUI pick them up next time."""
    patch: dict = {
        "format": cfg.fmt,
        "bitrate_kbps": cfg.bitrate_kbps,
        "cover_max_edge": cfg.cover_max_edge,
        "cover_quality": cfg.cover_quality,
        "keep": cfg.keep,
        "drop": cfg.drop,
        "embed_lrc": cfg.embed_lrc,
        "embed_txt": cfg.embed_txt,
        "dst_dir": str(dst_dir),
    }
    if jobs is not None:
        patch["jobs"] = jobs
    settings.save(patch)


@app.command()
def file(
    src: Path = typer.Argument(..., help="Input audio file."),
    dst: Path = typer.Argument(..., help="Output .opus/.ogg/.mp3 file."),
    fmt: Optional[str] = FmtOpt,
    bitrate: Optional[int] = BitrateOpt,
    cover_size: Optional[int] = CoverOpt,
    cover_quality: Optional[int] = QualityOpt,
    keep_all: bool = KeepAllOpt,
    keep: Optional[str] = KeepOpt,
    drop: Optional[str] = DropOpt,
    embed_lrc: Optional[bool] = EmbedLrcOpt,
    embed_txt: Optional[bool] = EmbedTxtOpt,
    overwrite: bool = OverwriteOpt,
    dry_run: bool = DryRunOpt,
):
    """Compress a single file."""
    cfg = _cfg(fmt, bitrate, cover_size, cover_quality, keep_all, keep, drop,
               embed_lrc, embed_txt)
    info = probe(src)
    console.print(
        f"[dim]in:[/] {info.audio_codec} "
        f"{(info.bit_rate or 0)//1000}kbps "
        f"cover={'yes' if info.has_cover else 'no'}"
        + (f" {info.cover_width}x{info.cover_height}" if info.has_cover else "")
    )
    if dry_run:
        console.print("[yellow]dry-run: nothing written.[/]")
        return
    # Duplicate prevention: same stem with any output suffix counts as done.
    if not overwrite:
        dup = existing_output(dst)
        if dup is not None:
            want, have = _display(Path(dst).name), _display(dup.name)
            console.print(
                f"[yellow]skip (exists): {want}"
                + (f" — already exported as {have}" if have != want else "")
                + " (use --overwrite).[/]"
            )
            return
    res = compress_one(src, dst, cfg, overwrite_sidecars=True)
    _remember(cfg, Path(dst).parent)
    parts = [
        f"[green]done[/] {res.in_bytes//1024}KB -> {res.out_bytes//1024}KB | cover: {res.cover_note}"
    ]
    if res.sidecar_note:
        parts.append(f"embedded: {res.sidecar_note}")
    if res.warnings:
        parts.append(f"[yellow]audio warning: {res.warnings}[/]")
    console.print(" | ".join(parts))


@app.command()
def batch(
    src_dir: Path = typer.Argument(..., help="Input folder (recursive)."),
    dst_dir: Path = typer.Argument(..., help="Output folder (mirrored tree)."),
    fmt: Optional[str] = FmtOpt,
    bitrate: Optional[int] = BitrateOpt,
    cover_size: Optional[int] = CoverOpt,
    cover_quality: Optional[int] = QualityOpt,
    keep_all: bool = KeepAllOpt,
    keep: Optional[str] = KeepOpt,
    drop: Optional[str] = DropOpt,
    embed_lrc: Optional[bool] = EmbedLrcOpt,
    embed_txt: Optional[bool] = EmbedTxtOpt,
    overwrite: bool = OverwriteOpt,
    dry_run: bool = DryRunOpt,
    jobs: Optional[int] = JobsOpt,
):
    """Compress a whole folder."""
    from rich.progress import Progress

    cfg = _cfg(fmt, bitrate, cover_size, cover_quality, keep_all, keep, drop,
               embed_lrc, embed_txt)
    n_jobs = _jobs(jobs)
    files = iter_inputs(src_dir)
    sidecars = iter_sidecars(src_dir)
    console.print(f"found {len(files)} file(s) -> .{cfg.fmt} @ {cfg.bitrate_kbps}kbps, cover<={cfg.cover_max_edge or 'orig'}px"
                  + (f" (+{len(sidecars)} sidecar .lrc/.txt)" if sidecars else "")
                  + f" [{n_jobs} workers]")
    if dry_run:
        table = Table("input", "codec", "cover")
        for f in files[:50]:
            info = probe(f)
            table.add_row(
                _display(f.name), str(info.audio_codec),
                f"{info.cover_width}x{info.cover_height}" if info.has_cover else "-",
            )
        console.print(table)
        if sidecars:
            console.print(f"[dim]{len(sidecars)} sidecar file(s) (.lrc/.txt) would be copied.[/]")
        return
    skip: set[str] = set()
    if cfg.embed_lrc:
        skip.add(".lrc")
    if cfg.embed_txt:
        skip.add(".txt")
    with Progress(transient=True) as progress:
        task = progress.add_task("compressing", total=len(files))
        results = compress_batch(
            src_dir, dst_dir, cfg, overwrite=overwrite, with_sidecars=False,
            jobs=n_jobs, progress_cb=lambda _d, _t: progress.update(task, completed=_d),
        )
    warned = sum(1 for r in results if r.warnings)
    embedded = sum(1 for r in results if r.sidecar_note)
    copied = copy_sidecars(src_dir, dst_dir, overwrite=overwrite, skip_suffixes=skip)
    skipped = 0 if overwrite else len(files) - len(results)
    if results:
        _remember(cfg, dst_dir, n_jobs)
        table = Table("file", "in", "out", "cover", "warning")
        for r in results:
            warn_cells = "; ".join(s for s in (r.warnings, r.sidecar_note) if s) or "-"
            table.add_row(_display(r.dst.name), f"{r.in_bytes//1024}KB", f"{r.out_bytes//1024}KB", _display(r.cover_note), _display(warn_cells))
        console.print(table)
        if skipped:
            console.print(
                f"[dim]skipped {skipped} file(s) already exported "
                f"(same name, any .opus/.ogg/.mp3 — use --overwrite).[/]"
            )
        if warned:
            console.print(
                f"[yellow]{warned} file(s) had audio decode warnings "
                f"(corrupt source frames concealed — output may glitch).[/]"
            )
        if embedded:
            console.print(f"[dim]{embedded} file(s) embedded sidecar text.[/]")
    else:
        console.print("[yellow]nothing to do (all outputs exist, use --overwrite).[/]")
    if copied:
        console.print(f"[dim]copied {len(copied)} sidecar file(s) (.lrc/.txt).[/]")


@app.command()
def tags(
    src: Path = typer.Argument(..., help="Audio file or folder to inspect."),
    limit: int = typer.Option(50, "--limit", "-n", help="Max files to scan."),
):
    """Show which tags your files actually have (with friendly names).

    Use the Tag column values with --keep/--drop, or pick them in the GUI.
    """
    files = iter_inputs(src)
    if not files:
        console.print("[yellow]no supported audio files found.[/]")
        return
    summary = collect_tag_summary(files, sample_files=limit)
    if not summary:
        console.print("[yellow]no tags found.[/]")
        return
    table = Table("Tag", "Name", "Example value", "Kept by default")
    for key, vals in summary.items():
        label, _desc = friendly_label(key)
        example = vals[0] if vals else ""
        if len(example) > 60:
            example = example[:57] + "..."
        table.add_row(key, label, _display(example), "yes" if key in DEFAULT_KEEP else "")
    console.print(f"[dim]{len(files)} file(s) scanned, {len(summary)} distinct tag(s):[/]")
    console.print(table)


def app_entry():  # console_scripts shim
    app()


if __name__ == "__main__":
    app()
