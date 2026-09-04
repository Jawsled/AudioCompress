"""Typer CLI: single file + batch folder modes."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.progress import track
from rich.table import Table

from . import settings
from .cover import COVER_PRESETS
from .extract import collect_tag_summary
from .metadata_map import DEFAULT_KEEP, friendly_label
from .pipeline import JobConfig, compress_batch, compress_one, iter_inputs
from .probe import probe

app = typer.Typer(add_completion=False, help="Compress audio, preserve cover art.")
console = Console()

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


def _parse_csv(v: Optional[str]) -> Optional[list[str]]:
    return [s.strip() for s in v.split(",") if s.strip()] if v else None


def _cfg(fmt, bitrate, cover_size, cover_quality, keep_all, keep, drop) -> JobConfig:
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
    return JobConfig(
        fmt=fmt,
        bitrate_kbps=bitrate,
        cover_max_edge=cover_size,
        cover_quality=cover_quality,
        keep_all=keep_all,
        keep=keep_list,
        drop=drop_list,
    )


def _remember(cfg: JobConfig, dst_dir: Path) -> None:
    """Persist last-used options so CLI and GUI pick them up next time."""
    settings.save({
        "format": cfg.fmt,
        "bitrate_kbps": cfg.bitrate_kbps,
        "cover_max_edge": cfg.cover_max_edge,
        "cover_quality": cfg.cover_quality,
        "keep": cfg.keep,
        "drop": cfg.drop,
        "dst_dir": str(dst_dir),
    })


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
    dry_run: bool = DryRunOpt,
):
    """Compress a single file."""
    cfg = _cfg(fmt, bitrate, cover_size, cover_quality, keep_all, keep, drop)
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
    res = compress_one(src, dst, cfg)
    _remember(cfg, Path(dst).parent)
    console.print(
        f"[green]done[/] {res.in_bytes//1024}KB -> {res.out_bytes//1024}KB | cover: {res.cover_note}"
    )


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
    overwrite: bool = OverwriteOpt,
    dry_run: bool = DryRunOpt,
):
    """Compress a whole folder."""
    cfg = _cfg(fmt, bitrate, cover_size, cover_quality, keep_all, keep, drop)
    files = iter_inputs(src_dir)
    console.print(f"found {len(files)} file(s) -> .{cfg.fmt} @ {cfg.bitrate_kbps}kbps, cover<={cfg.cover_max_edge or 'orig'}px")
    if dry_run:
        table = Table("input", "codec", "cover")
        for f in files[:50]:
            info = probe(f)
            table.add_row(
                f.name, str(info.audio_codec),
                f"{info.cover_width}x{info.cover_height}" if info.has_cover else "-",
            )
        console.print(table)
        return
    results = []
    for src in track(files, description="compressing"):
        rel = src.relative_to(src_dir) if src_dir.is_dir() else Path(src.name)
        dst = (dst_dir / rel).with_suffix(f".{cfg.fmt}")
        if dst.exists() and not overwrite:
            continue
        results.append(compress_one(src, dst, cfg))
    if results:
        _remember(cfg, dst_dir)
        table = Table("file", "in", "out", "cover")
        for r in results:
            table.add_row(r.dst.name, f"{r.in_bytes//1024}KB", f"{r.out_bytes//1024}KB", r.cover_note)
        console.print(table)
    else:
        console.print("[yellow]nothing to do (all outputs exist, use --overwrite).[/]")


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
        table.add_row(key, label, example, "yes" if key in DEFAULT_KEEP else "")
    console.print(f"[dim]{len(files)} file(s) scanned, {len(summary)} distinct tag(s):[/]")
    console.print(table)


def app_entry():  # console_scripts shim
    app()


if __name__ == "__main__":
    app()
