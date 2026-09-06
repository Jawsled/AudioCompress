"""Minimal cross-platform GUI around the audiocompress pipeline.

Stdlib `tkinter` only — no extra dependencies, works on Windows/macOS/Linux.
Run with:
    python -m audiocompress.gui
    python audiocompress.py gui
"""

from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import settings
from .chime import play_chime
from .cover import COVER_PRESETS
from .extract import collect_tag_summary
from .metadata_map import DEFAULT_KEEP, friendly_label
from .pipeline import JobConfig, compress_one, copy_sidecars, default_jobs, existing_output, iter_inputs
from .probe import probe

COVER_LABELS = ["Original (keep bytes)", *[str(p) for p in sorted(COVER_PRESETS)]]
BITRATES = ["96", "128", "160", "192", "256", "320"]
FORMATS = ["opus", "ogg", "mp3"]


def _cover_to_int(label: str) -> int:
    return 0 if label.startswith("Original") else int(label)


def _parse_csv(v: str) -> list[str] | None:
    items = [s.strip() for s in v.split(",") if s.strip()]
    return items or None


def _native(path: str) -> str:
    """OS-native separators (file dialogs return `/` even on Windows)."""
    return os.path.normpath(path) if path else path


class App(ttk.Frame):
    def __init__(self, root: tk.Tk) -> None:
        super().__init__(root, padding=10)
        self.root = root
        self.pack(fill="both", expand=True)
        self._queue: queue.Queue = queue.Queue()
        self._running = False
        self._saved = settings.load()
        # None = default whitelist; otherwise explicit list of tag keys.
        self.tags_keep: list[str] | None = self._saved.get("keep")
        self._build()
        self._poll()

    def _saved_str(self, key: str, default: str) -> str:
        v = self._saved.get(key, default)
        return str(v) if v is not None else default

    # -- layout ---------------------------------------------------------
    def _build(self) -> None:
        r = 0
        ttk.Label(self, text="Source (file or folder):").grid(row=r, column=0, sticky="w")
        self.src_var = tk.StringVar(value=_native(self._saved.get("src_dir", "")))
        ttk.Entry(self, textvariable=self.src_var, width=55).grid(row=r, column=1, sticky="ew")
        self._last_src_dir = self.src_var.get()
        ttk.Button(self, text="File…", command=self._pick_file).grid(row=r, column=2)
        ttk.Button(self, text="Folder…", command=self._pick_folder_src).grid(row=r, column=3)
        r += 1
        ttk.Label(self, text="Destination folder:").grid(row=r, column=0, sticky="w")
        self.dst_var = tk.StringVar(value=_native(self._saved.get("dst_dir", "")))
        ttk.Entry(self, textvariable=self.dst_var, width=55).grid(row=r, column=1, sticky="ew")
        ttk.Button(self, text="Browse…", command=self._pick_folder_dst).grid(row=r, column=2)
        r += 1

        opts = ttk.LabelFrame(self, text="Options", padding=8)
        opts.grid(row=r, column=0, columnspan=4, sticky="ew", pady=8)
        for i in range(4):
            opts.columnconfigure(i, weight=1)

        ttk.Label(opts, text="Format:").grid(row=0, column=0, sticky="w")
        self.fmt_var = tk.StringVar(value=self._saved_str("format", "opus"))
        ttk.Combobox(opts, textvariable=self.fmt_var, values=FORMATS,
                     state="readonly", width=10).grid(row=0, column=1, sticky="w")

        ttk.Label(opts, text="Bitrate (kbps):").grid(row=0, column=2, sticky="w")
        self.bitrate_var = tk.StringVar(value=self._saved_str("bitrate_kbps", "160"))
        ttk.Combobox(opts, textvariable=self.bitrate_var, values=BITRATES,
                     state="readonly", width=10).grid(row=0, column=3, sticky="w")

        ttk.Label(opts, text="Cover max edge:").grid(row=1, column=0, sticky="w")
        edge = self._saved.get("cover_max_edge", 1000)
        self.cover_var = tk.StringVar(
            value="Original (keep bytes)" if edge == 0 else str(edge))
        ttk.Combobox(opts, textvariable=self.cover_var, values=COVER_LABELS,
                     state="readonly", width=22).grid(row=1, column=1, sticky="w")

        ttk.Label(opts, text="JPEG quality:").grid(row=1, column=2, sticky="w")
        self.quality_var = tk.IntVar(value=int(self._saved.get("cover_quality", 93)))
        ttk.Spinbox(opts, from_=70, to=100, textvariable=self.quality_var, width=10).grid(
            row=1, column=3, sticky="w")

        ttk.Label(opts, text="Tags:").grid(row=2, column=0, sticky="w")
        ttk.Button(opts, text="Select", command=self._choose_tags).grid(
            row=2, column=1, sticky="w")
        self.tags_var = tk.StringVar()
        self._refresh_tags_label()
        ttk.Label(opts, textvariable=self.tags_var).grid(row=2, column=2, sticky="w")

        ttk.Label(opts, text="Drop tags (csv):").grid(row=3, column=0, sticky="w")
        self.drop_var = tk.StringVar(value=", ".join(self._saved.get("drop") or []))
        ttk.Entry(opts, textvariable=self.drop_var, width=24).grid(row=3, column=1, sticky="w")

        self.keep_all_var = tk.BooleanVar(value=bool(self._saved.get("keep_all", False)))
        ttk.Checkbutton(opts, text="Keep all tags", variable=self.keep_all_var).grid(
            row=3, column=2, sticky="w")
        self.overwrite_var = tk.BooleanVar(value=bool(self._saved.get("overwrite", False)))
        ttk.Checkbutton(opts, text="Overwrite existing", variable=self.overwrite_var).grid(
            row=3, column=3, sticky="w")

        ttk.Label(opts, text="Sidecars:").grid(row=4, column=0, sticky="w")
        self.embed_lrc_var = tk.BooleanVar(value=bool(self._saved.get("embed_lrc", False)))
        ttk.Checkbutton(opts, text="Embed .lrc → lyrics", variable=self.embed_lrc_var).grid(
            row=4, column=1, sticky="w")
        self.embed_txt_var = tk.BooleanVar(value=bool(self._saved.get("embed_txt", False)))
        ttk.Checkbutton(opts, text="Embed .txt → comment", variable=self.embed_txt_var).grid(
            row=4, column=2, sticky="w")
        # Workers label directly left of its number: parallel files at once.
        workers_frame = ttk.Frame(opts)
        workers_frame.grid(row=4, column=3, sticky="w")
        ttk.Label(workers_frame, text="Workers:").pack(side="left")
        self.jobs_var = tk.IntVar(value=int(self._saved.get("jobs", default_jobs())))
        ttk.Spinbox(workers_frame, from_=1, to=32, textvariable=self.jobs_var, width=5).pack(
            side="left", padx=(4, 0))
        r += 1

        btns = ttk.Frame(self)
        btns.grid(row=r, column=0, columnspan=4, sticky="ew", pady=4)
        self.preview_btn = ttk.Button(btns, text="Preview", command=self._preview)
        self.preview_btn.pack(side="left", padx=4)
        self.run_btn = ttk.Button(btns, text="Compress", command=self._run)
        self.run_btn.pack(side="left", padx=4)
        self.chime_var = tk.BooleanVar(value=bool(self._saved.get("chime", True)))
        self.chime_box = ttk.Checkbutton(btns, text="Chime", variable=self.chime_var)
        self.chime_box.pack(side="left", padx=4)
        self.status_var = tk.StringVar(value="idle")
        ttk.Label(btns, textvariable=self.status_var).pack(side="right")
        r += 1

        self.progress = ttk.Progressbar(self, mode="determinate")
        self.progress.grid(row=r, column=0, columnspan=4, sticky="ew", pady=4)
        r += 1

        self.log = tk.Text(self, height=14, wrap="word", state="disabled")
        self.log.grid(row=r, column=0, columnspan=4, sticky="nsew")
        self.rowconfigure(r, weight=1)
        self.columnconfigure(1, weight=1)

    # -- pickers --------------------------------------------------------
    def _pick_file(self) -> None:
        initial = self.src_var.get() or self._saved.get("src_dir", "")
        p = filedialog.askopenfilename(initialdir=initial or None)
        if p:
            self.src_var.set(_native(p))
            self._last_src_dir = str(Path(p).parent)
            settings.save({"src_dir": self._last_src_dir})

    def _pick_folder_src(self) -> None:
        initial = self.src_var.get() or self._saved.get("src_dir", "")
        p = filedialog.askdirectory(initialdir=initial or None)
        if p:
            self.src_var.set(_native(p))
            settings.save({"src_dir": p})

    def _pick_folder_dst(self) -> None:
        initial = self.dst_var.get() or self._saved.get("dst_dir", "")
        p = filedialog.askdirectory(initialdir=initial or None)
        if p:
            self.dst_var.set(_native(p))
            settings.save({"dst_dir": p})

    # -- config ---------------------------------------------------------
    def _config(self) -> JobConfig:
        return JobConfig(
            fmt=self.fmt_var.get().lower(),
            bitrate_kbps=int(self.bitrate_var.get()),
            cover_max_edge=_cover_to_int(self.cover_var.get()),
            cover_quality=int(self.quality_var.get()),
            keep_all=self.keep_all_var.get(),
            keep=self.tags_keep,
            drop=_parse_csv(self.drop_var.get()),
            embed_lrc=self.embed_lrc_var.get(),
            embed_txt=self.embed_txt_var.get(),
        )

    def _refresh_tags_label(self) -> None:
        if self.tags_keep is None:
            self.tags_var.set(f"Defaults ({len(DEFAULT_KEEP)} tags)")
        else:
            self.tags_var.set(f"Custom ({len(self.tags_keep)} selected)")

    def _remember(self, cfg: JobConfig, out_dir: Path) -> None:
        settings.save({
            "format": cfg.fmt,
            "bitrate_kbps": cfg.bitrate_kbps,
            "cover_max_edge": cfg.cover_max_edge,
            "cover_quality": cfg.cover_quality,
            "keep_all": cfg.keep_all,
            "keep": cfg.keep,
            "drop": cfg.drop,
            "embed_lrc": cfg.embed_lrc,
            "embed_txt": cfg.embed_txt,
            "jobs": int(self.jobs_var.get()),
            "overwrite": self.overwrite_var.get(),
            "chime": self.chime_var.get(),
            "dst_dir": str(out_dir),
        })

    def _choose_tags(self) -> None:
        src = self.src_var.get().strip()
        if not src:
            messagebox.showinfo("Tags", "Pick a source file or folder first.")
            return
        try:
            files = iter_inputs(Path(src))
        except Exception as exc:
            messagebox.showwarning("Tags", str(exc))
            return
        if not files:
            messagebox.showinfo("Tags", "No supported audio files found.")
            return
        summary = collect_tag_summary(files)
        if not summary:
            messagebox.showinfo("Tags", "No tags found in these files.")
            return
        dlg = tk.Toplevel(self.root)
        dlg.title("Choose tags to keep")
        dlg.geometry("520x420")
        dlg.transient(self.root)
        ttk.Label(
            dlg,
            text="Tick the tags to keep. Friendly names are shown —\n"
                 "the raw key is in brackets.",
            padding=8,
        ).pack(anchor="w")

        canvas = tk.Canvas(dlg)
        scroll = ttk.Scrollbar(dlg, orient="vertical", command=canvas.yview)
        body = ttk.Frame(canvas)
        body.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bounding_box("all")))
        canvas.create_window((0, 0), window=body, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        current = set(self.tags_keep) if self.tags_keep is not None else set(DEFAULT_KEEP)
        states: dict[str, tk.BooleanVar] = {}
        for key, vals in summary.items():
            label, _desc = friendly_label(key)
            example = vals[0] if vals else ""
            if len(example) > 50:
                example = example[:47] + "..."
            var = tk.BooleanVar(value=key in current)
            states[key] = var
            row = ttk.Frame(body, padding=(8, 2))
            row.pack(fill="x")
            ttk.Checkbutton(row, variable=var).pack(side="left")
            ttk.Label(row, text=f"{label}  [{key}]", width=34).pack(side="left")
            ttk.Label(row, text=example, foreground="gray").pack(side="left")

        btns = ttk.Frame(dlg, padding=8)
        btns.pack(fill="x")
        ttk.Button(btns, text="Defaults",
                   command=lambda: [v.set(k in DEFAULT_KEEP) for k, v in states.items()]).pack(side="left")
        ttk.Button(btns, text="All",
                   command=lambda: [v.set(True) for v in states.values()]).pack(side="left", padx=4)
        ttk.Button(btns, text="None",
                   command=lambda: [v.set(False) for v in states.values()]).pack(side="left")

        def _ok() -> None:
            self.tags_keep = sorted(k for k, v in states.items() if v.get())
            self._refresh_tags_label()
            dlg.destroy()

        ttk.Button(btns, text="OK", command=_ok).pack(side="right")
        ttk.Button(btns, text="Cancel", command=dlg.destroy).pack(side="right", padx=4)
        dlg.grab_set()
        dlg.wait_window()

    def _targets(self) -> tuple[list[Path], Path, JobConfig]:
        src = self.src_var.get().strip()
        dst = self.dst_var.get().strip()
        if not src:
            raise ValueError("pick a source file or folder first")
        src_p = Path(src)
        files = iter_inputs(src_p)
        if not files:
            raise ValueError("no supported audio files found in source")
        cfg = self._config()
        if src_p.is_file():
            if not dst:
                raise ValueError("pick a destination folder (or file path)")
            dst_p = Path(dst)
            out_dir = dst_p if dst_p.suffix.lower() not in (".opus", ".ogg", ".mp3") else dst_p.parent
        else:
            if not dst:
                raise ValueError("pick a destination folder")
            out_dir = Path(dst)
        return files, out_dir, cfg

    # -- actions --------------------------------------------------------
    def _preview(self) -> None:
        try:
            files, _, cfg = self._targets()
        except ValueError as exc:
            messagebox.showwarning("Preview", str(exc))
            return
        keep = "all" if cfg.keep_all else (
            f"{len(cfg.keep)} selected" if cfg.keep is not None
            else f"defaults ({len(DEFAULT_KEEP)})")
        lines = [f"{len(files)} file(s) -> .{cfg.fmt} @ {cfg.bitrate_kbps}kbps | tags: {keep}"]
        embed = [s for s, on in ((".lrc→lyrics", cfg.embed_lrc), (".txt→comment", cfg.embed_txt)) if on]
        if embed:
            lines[0] += f" | embed {', '.join(embed)}"
        lines[0] += f" | {int(self.jobs_var.get())} workers"
        try:
            info = probe(files[0])
            cover = (f"{info.cover_width}x{info.cover_height}"
                     if info.has_cover else "no cover")
            lines.append(f"first: {files[0].name} [{info.audio_codec}] {cover}")
        except Exception as exc:  # keep preview non-fatal
            lines.append(f"probe failed for first file: {exc}")
        self._log("\n".join(lines) + "\n")

    def _run(self) -> None:
        if self._running:
            return
        try:
            files, out_dir, cfg = self._targets()
        except ValueError as exc:
            messagebox.showwarning("Compress", str(exc))
            return
        src_root = Path(self.src_var.get().strip())
        self._running = True
        self._remember(cfg, out_dir)
        self.run_btn.state(["disabled"])
        self.progress.configure(maximum=len(files), value=0)
        self.status_var.set(f"0/{len(files)}")
        threading.Thread(
            target=self._worker,
            args=(files, src_root, out_dir, cfg, self.overwrite_var.get(),
                  int(self.jobs_var.get())),
            daemon=True,
        ).start()

    def _one_line(self, src: Path, out_dir: Path, src_root: Path,
                  cfg: JobConfig, overwrite: bool) -> str:
        from pathlib import Path as _P

        rel = src.relative_to(src_root) if src_root.is_dir() else _P(src.name)
        dst = (out_dir / rel).with_suffix(f".{cfg.fmt}")
        if not overwrite:
            dup = existing_output(dst)
            if dup is not None:
                detail = f" (found {dup.name})" if dup.name != dst.name else ""
                return f"skip (exists): {src.name}{detail}\n"
        res = compress_one(src, dst, cfg, overwrite_sidecars=overwrite)
        line = (f"ok: {res.dst.name} "
                f"{res.in_bytes//1024}KB -> {res.out_bytes//1024}KB "
                f"| {res.cover_note}")
        if res.sidecar_note:
            line += f" | embedded {res.sidecar_note}"
        if res.warnings:
            line += f" | WARNING (source corrupt?): {res.warnings}"
        return line + "\n"

    def _worker(self, files: list[Path], src_root: Path, out_dir: Path,
                cfg: JobConfig, overwrite: bool, jobs: int | None = None) -> None:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        n_workers = default_jobs(jobs)
        done = errors = 0
        if n_workers <= 1 or len(files) <= 1:
            for src in files:
                try:
                    self._queue.put(("log", self._one_line(src, out_dir, src_root, cfg, overwrite)))
                    done += 1
                except Exception as exc:  # per-file errors shouldn't kill the batch
                    errors += 1
                    self._queue.put(("log", f"FAIL {src.name}: {exc}\n"))
                self._queue.put(("progress", done, len(files)))
        else:
            with ThreadPoolExecutor(max_workers=n_workers) as pool:
                futs = {pool.submit(
                    self._one_line, src, out_dir, src_root, cfg, overwrite): src
                    for src in files}
                for fut in as_completed(futs):
                    src = futs[fut]
                    try:
                        self._queue.put(("log", fut.result()))
                        done += 1
                    except Exception as exc:
                        errors += 1
                        self._queue.put(("log", f"FAIL {src.name}: {exc}\n"))
                    self._queue.put(("progress", done, len(files)))
        try:
            skip: set[str] = set()
            if cfg.embed_lrc:
                skip.add(".lrc")
            if cfg.embed_txt:
                skip.add(".txt")
            copied = copy_sidecars(src_root, out_dir, overwrite=overwrite, skip_suffixes=skip)
            if copied:
                self._queue.put(("log", f"copied {len(copied)} sidecar file(s) (.lrc/.txt)\n"))
        except Exception as exc:
            self._queue.put(("log", f"sidecar copy failed: {exc}\n"))
        self._queue.put(("done", done, errors))

    # -- ui thread plumbing ---------------------------------------------
    def _poll(self) -> None:
        try:
            while True:
                msg = self._queue.get_nowait()
                kind = msg[0]
                if kind == "log":
                    self._log(msg[1])
                elif kind == "progress":
                    _, done, total = msg
                    self.progress.configure(value=done)
                    self.status_var.set(f"{done}/{total}")
                elif kind == "done":
                    _, done, errors = msg
                    self._running = False
                    self.run_btn.state(["!disabled"])
                    self.status_var.set(f"done: {done} file(s), {errors} error(s)")
                    try:
                        # Distinct generated chime only — never the OS bell.
                        if self.chime_var.get():
                            play_chime()
                    except Exception:
                        pass
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")


def main() -> None:
    root = tk.Tk()
    root.title("Audiocompress")
    root.geometry("720x560")
    try:
        root.tk.call("tk", "windowingsystem")  # no-op, keeps linters quiet
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
