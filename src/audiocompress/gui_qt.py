"""Qt (PySide6) GUI around the audiocompress pipeline.

Feature-parity port of ``audiocompress.gui`` (tkinter): same options, same
defaults, same settings keys, same log line format. Needs the ``gui`` extra:
``pip install -e ".[gui]"``. Run with ``python audiocompress.py gui`` (prefers
this over tkinter when PySide6 is installed) or ``python -m
audiocompress.gui_qt``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import settings
from .cover import COVER_PRESETS
from .extract import collect_tag_summary
from .metadata_map import DEFAULT_KEEP, detect_batch_format, friendly_label, native_key
from .pipeline import (
    JobConfig,
    compress_one,
    copy_sidecars,
    default_jobs,
    iter_inputs,
)
from .probe import probe

FORMATS = ["opus", "ogg", "mp3"]
BITRATES = ["96", "128", "160", "192", "256", "320"]
COVER_LABELS = ["Original", *[str(p) for p in sorted(COVER_PRESETS)]]


def _cover_to_int(label: str) -> int:
    return 0 if label.startswith("Original") else int(label)


def _parse_csv(v: str) -> list[str] | None:
    items = [s.strip() for s in v.split(",") if s.strip()]
    return items or None


def _native(path: str) -> str:
    """OS-native separators (file dialogs return `/` even on Windows)."""
    return os.path.normpath(path) if path else path


class CompressWorker(QThread):
    """Batch runner (thread pool). Per-file errors are reported, never fatal."""

    message = Signal(str)
    progress = Signal(int, int)
    finished_ok = Signal(int, int)  # done, errors

    def __init__(self, files: list[Path], src_root: Path, out_dir: Path,
                 cfg: JobConfig, overwrite: bool, jobs: int | None = None) -> None:
        super().__init__()
        self._files = files
        self._src_root = src_root
        self._out_dir = out_dir
        self._cfg = cfg
        self._overwrite = overwrite
        self._jobs = default_jobs(jobs)

    def _dst_for(self, src: Path) -> Path:
        rel = src.relative_to(self._src_root) if self._src_root.is_dir() else Path(src.name)
        return (self._out_dir / rel).with_suffix(f".{self._cfg.fmt}")

    def _one(self, src: Path) -> str:
        dst = self._dst_for(src)
        if dst.exists() and not self._overwrite:
            return f"skip (exists): {src.name}\n"
        res = compress_one(src, dst, self._cfg, overwrite_sidecars=self._overwrite)
        line = (
            f"ok: {res.dst.name} "
            f"{res.in_bytes // 1024}KB -> {res.out_bytes // 1024}KB "
            f"| {res.cover_note}")
        if res.sidecar_note:
            line += f" | embedded {res.sidecar_note}"
        if res.warnings:
            line += f" | WARNING (source corrupt?): {res.warnings}"
        return line + "\n"

    def run(self) -> None:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        done = errors = 0
        total = len(self._files)
        if self._jobs <= 1 or total <= 1:
            for src in self._files:
                try:
                    self.message.emit(self._one(src))
                    done += 1
                except Exception as exc:
                    errors += 1
                    self.message.emit(f"FAIL {src.name}: {exc}\n")
                self.progress.emit(done, total)
        else:
            with ThreadPoolExecutor(max_workers=self._jobs) as pool:
                futs = {pool.submit(self._one, src): src for src in self._files}
                for fut in as_completed(futs):
                    src = futs[fut]
                    try:
                        self.message.emit(fut.result())
                        done += 1
                    except Exception as exc:
                        errors += 1
                        self.message.emit(f"FAIL {src.name}: {exc}\n")
                    self.progress.emit(done, total)
        try:
            skip: set[str] = set()
            if self._cfg.embed_lrc:
                skip.add(".lrc")
            if self._cfg.embed_txt:
                skip.add(".txt")
            copied = copy_sidecars(
                self._src_root, self._out_dir,
                overwrite=self._overwrite, skip_suffixes=skip,
            )
            if copied:
                self.message.emit(f"copied {len(copied)} sidecar file(s) (.lrc/.txt)\n")
        except Exception as exc:
            self.message.emit(f"sidecar copy failed: {exc}\n")
        self.finished_ok.emit(done, errors)


class TagDialog(QDialog):
    """Searchable checklist of tags found in the source files."""

    def __init__(self, parent: QWidget, summary: dict[str, list[str]],
                 checked: set[str], key_to_format: dict[str, str] | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Choose tags to keep")
        self.resize(820, 460)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Tick the tags to keep. Friendly name, native tag name, container, example value."))
        self._search = QLineEdit(self)
        self._search.setPlaceholderText("Filter…")
        self._search.textChanged.connect(self._apply_filter)
        layout.addWidget(self._search)

        self._table = QTableWidget(0, 5, self)
        self._table.setHorizontalHeaderLabels(["", "Name", "Tag", "Container", "Example value"])
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.Stretch)

        for key, vals in summary.items():
            label, _desc = friendly_label(key)
            example = vals[0] if vals else ""
            fmt = (key_to_format or {}).get(key, "Other")
            native = native_key(key, fmt) or key
            tag_cell = f"{native}" if fmt == "Other" else f"{native} ({fmt})"
            row = self._table.rowCount()
            self._table.insertRow(row)
            check_item = QTableWidgetItem()
            check_item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled
                                | Qt.ItemIsSelectable)
            check_item.setCheckState(Qt.Checked if key in checked else Qt.Unchecked)
            check_item.setData(Qt.UserRole, key)
            self._table.setItem(row, 0, check_item)
            self._table.setItem(row, 1, QTableWidgetItem(label))
            self._table.setItem(row, 2, QTableWidgetItem(tag_cell))
            self._table.setItem(row, 3, QTableWidgetItem(fmt))
            self._table.setItem(row, 4, QTableWidgetItem(example))
        layout.addWidget(self._table)

        row = QHBoxLayout()
        for text in ("Defaults", "All", "None"):
            btn = QPushButton(text)
            btn.clicked.connect(lambda _=False, t=text: self._bulk(t))
            row.addWidget(btn)
        row.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        row.addWidget(buttons)
        layout.addLayout(row)

    def _bulk(self, mode: str) -> None:
        for i in range(self._table.rowCount()):
            check_item = self._table.item(i, 0)
            if mode == "All":
                check_item.setCheckState(Qt.Checked)
            elif mode == "None":
                check_item.setCheckState(Qt.Unchecked)
            else:  # Defaults
                check_item.setCheckState(
                    Qt.Checked if check_item.data(Qt.UserRole) in DEFAULT_KEEP
                    else Qt.Unchecked)

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        for i in range(self._table.rowCount()):
            cells = (self._table.item(i, c).text().lower()
                     for c in (1, 2, 3, 4)
                     if self._table.item(i, c) is not None)
            self._table.setRowHidden(i, bool(needle) and needle not in " ".join(cells))

    def selected(self) -> list[str]:
        out = []
        for i in range(self._table.rowCount()):
            item = self._table.item(i, 0)
            if item.checkState() == Qt.Checked:
                out.append(item.data(Qt.UserRole))
        return sorted(out)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Audiocompress")
        self.resize(720, 560)
        self._saved = settings.load()
        # None = default whitelist; otherwise explicit list of tag keys.
        self.tags_keep: list[str] | None = self._saved.get("keep")
        self._worker: CompressWorker | None = None

        central = QWidget(self)
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        # Source / destination.
        paths = QGridLayout()
        paths.addWidget(QLabel("Source:"), 0, 0)
        self._src = QLineEdit(_native(self._saved.get("src_dir", "")))
        paths.addWidget(self._src, 0, 1)
        btn_file = QPushButton("File…")
        btn_file.clicked.connect(self._pick_file)
        paths.addWidget(btn_file, 0, 2)
        btn_folder = QPushButton("Folder…")
        btn_folder.clicked.connect(lambda: self._pick_dir(self._src, persist="src_dir"))
        paths.addWidget(btn_folder, 0, 3)
        paths.addWidget(QLabel("Destination folder:"), 1, 0)
        self._dst = QLineEdit(_native(self._saved.get("dst_dir", "")))
        paths.addWidget(self._dst, 1, 1)
        btn_dst = QPushButton("Browse…")
        btn_dst.clicked.connect(lambda: self._pick_dir(self._dst, persist="dst_dir"))
        paths.addWidget(btn_dst, 1, 2)
        layout.addLayout(paths)

        # Options.
        opts = QGroupBox("Options")
        grid = QGridLayout(opts)
        grid.addWidget(QLabel("Format:"), 0, 0)
        self._fmt = QComboBox()
        self._fmt.addItems(FORMATS)
        self._fmt.setCurrentText(str(self._saved.get("format", "opus")))
        grid.addWidget(self._fmt, 0, 1)
        grid.addWidget(QLabel("Bitrate:"), 0, 2)
        self._bitrate = QComboBox()
        self._bitrate.setToolTip("Audio bitrate in kbps (32-320)")
        self._bitrate.addItems(BITRATES)
        self._bitrate.setCurrentText(str(self._saved.get("bitrate_kbps", "160")))
        grid.addWidget(self._bitrate, 0, 3)

        cover_label = QLabel("Cover size:")
        cover_label.setToolTip("Cover long-edge preset in px, or Original to keep bytes")
        grid.addWidget(cover_label, 1, 0)
        self._cover = QComboBox()
        self._cover.addItems(COVER_LABELS)
        edge = self._saved.get("cover_max_edge", 1000)
        self._cover.setCurrentText("Original" if edge == 0 else str(edge))
        grid.addWidget(self._cover, 1, 1)
        quality_label = QLabel("Quality:")
        quality_label.setToolTip("JPEG quality for resized covers")
        grid.addWidget(quality_label, 1, 2)
        self._quality = QSpinBox()
        self._quality.setRange(70, 100)
        self._quality.setValue(int(self._saved.get("cover_quality", 93)))
        grid.addWidget(self._quality, 1, 3)

        self._tags_label = QLabel()
        self._tags_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self._refresh_tags_label()
        # Left-aligned: [Select] [status text], spanning columns 1-3.
        tags_row = QHBoxLayout()
        tags_row.setContentsMargins(0, 0, 0, 0)
        tags_row.setSpacing(8)
        btn_tags = QPushButton("Select")
        btn_tags.setFixedWidth(86)  # match the other action buttons
        btn_tags.clicked.connect(self._choose_tags)
        tags_row.addWidget(btn_tags)
        tags_row.addWidget(self._tags_label, 0, Qt.AlignLeft)
        tags_row.addStretch(1)
        container = QWidget()
        container.setLayout(tags_row)
        grid.addWidget(QLabel("Tags:"), 2, 0)
        grid.addWidget(container, 2, 1, 1, 3)

        drop_label = QLabel("Drop tags:")
        drop_label.setToolTip("Comma-separated tags to drop")
        grid.addWidget(drop_label, 3, 0)
        self._drop = QLineEdit(", ".join(self._saved.get("drop") or []))
        grid.addWidget(self._drop, 3, 1)
        self._keep_all = QCheckBox("Keep all tags")
        self._keep_all.setChecked(bool(self._saved.get("keep_all", False)))
        grid.addWidget(self._keep_all, 3, 2)
        self._overwrite = QCheckBox("Overwrite")
        self._overwrite.setToolTip("Overwrite existing outputs")
        self._overwrite.setChecked(bool(self._saved.get("overwrite", False)))
        grid.addWidget(self._overwrite, 3, 3)
        self._embed_lrc = QCheckBox("Embed .lrc → lyrics")
        self._embed_lrc.setToolTip("Embed same-stem .lrc file into the lyrics tag instead of copying it")
        self._embed_lrc.setChecked(bool(self._saved.get("embed_lrc", False)))
        grid.addWidget(self._embed_lrc, 4, 1)
        self._embed_txt = QCheckBox("Embed .txt → comment")
        self._embed_txt.setToolTip("Embed same-stem .txt file into the comment tag instead of copying it")
        self._embed_txt.setChecked(bool(self._saved.get("embed_txt", False)))
        grid.addWidget(self._embed_txt, 4, 2)
        sidecars_label = QLabel("Sidecars:")
        sidecars_label.setToolTip("Tick to embed sidecar files into tags instead of copying them")
        grid.addWidget(sidecars_label, 4, 0)
        # Workers label sits directly left of its number: parallel files at once.
        workers_box = QWidget()
        workers_row = QHBoxLayout(workers_box)
        workers_row.setContentsMargins(0, 0, 0, 0)
        workers_row.setSpacing(6)
        workers_label = QLabel("Workers:")
        workers_label.setToolTip("Parallel files converted at once (worker threads). Higher = faster, more CPU/RAM.")
        workers_row.addWidget(workers_label)
        self._jobs = QSpinBox()
        self._jobs.setRange(1, 32)
        self._jobs.setToolTip("Parallel files converted at once (worker threads). Higher = faster, more CPU/RAM.")
        self._jobs.setValue(int(self._saved.get("jobs", default_jobs())))
        workers_row.addWidget(self._jobs)
        workers_row.addStretch(1)
        grid.addWidget(workers_box, 4, 3)
        layout.addWidget(opts)

        # Actions + progress + log.
        row = QHBoxLayout()
        btn_preview = QPushButton("Preview")
        btn_preview.clicked.connect(self._preview)
        row.addWidget(btn_preview)
        self._run_btn = QPushButton("Compress")
        self._run_btn.clicked.connect(self._run)
        row.addWidget(self._run_btn)
        row.addStretch(1)
        self._status = QLabel("idle")
        row.addWidget(self._status)
        layout.addLayout(row)

        self._progress = QProgressBar()
        layout.addWidget(self._progress)
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        layout.addWidget(self._log, 1)

    # -- pickers ------------------------------------------------------
    def _pick_file(self) -> None:
        start = self._src.text().strip() or self._saved.get("src_dir", "")
        path, _ = QFileDialog.getOpenFileName(self, "Pick audio file", start)
        if path:
            self._src.setText(_native(path))
            settings.save({"src_dir": str(Path(path).parent)})

    def _pick_dir(self, target: QLineEdit, persist: str | None = None) -> None:
        start = target.text().strip() or self._saved.get(persist or "", "")
        path = QFileDialog.getExistingDirectory(self, "Pick folder", start)
        if path:
            target.setText(_native(path))
            if persist:
                settings.save({persist: path})

    # -- config -------------------------------------------------------
    def _config(self) -> JobConfig:
        return JobConfig(
            fmt=self._fmt.currentText().lower(),
            bitrate_kbps=int(self._bitrate.currentText()),
            cover_max_edge=_cover_to_int(self._cover.currentText()),
            cover_quality=int(self._quality.value()),
            keep_all=self._keep_all.isChecked(),
            keep=self.tags_keep,
            drop=_parse_csv(self._drop.text()),
            embed_lrc=self._embed_lrc.isChecked(),
            embed_txt=self._embed_txt.isChecked(),
        )

    def _refresh_tags_label(self) -> None:
        if self.tags_keep is None:
            self._tags_label.setText(f"Default ({len(DEFAULT_KEEP)})")
        else:
            self._tags_label.setText(f"Custom ({len(self.tags_keep)})")

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
            "jobs": int(self._jobs.value()),
            "overwrite": self._overwrite.isChecked(),
            "dst_dir": str(out_dir),
        })

    def _targets(self) -> tuple[list[Path], Path, JobConfig]:
        src = self._src.text().strip()
        dst = self._dst.text().strip()
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

    def _choose_tags(self) -> None:
        src = self._src.text().strip()
        if not src:
            QMessageBox.information(self, "Tags", "Pick a source file or folder first.")
            return
        try:
            files = iter_inputs(Path(src))
        except Exception as exc:  # keep dialog errors non-fatal
            QMessageBox.warning(self, "Tags", str(exc))
            return
        if not files:
            QMessageBox.information(self, "Tags", "No supported audio files found.")
            return
        summary = collect_tag_summary(files)
        if not summary:
            QMessageBox.information(self, "Tags", "No tags found in these files.")
            return
        checked = set(self.tags_keep) if self.tags_keep is not None else set(DEFAULT_KEEP)
        key_to_format = detect_batch_format(files)
        dlg = TagDialog(self, summary, checked, key_to_format)
        if dlg.exec():
            self.tags_keep = dlg.selected()
            self._refresh_tags_label()

    # -- actions ------------------------------------------------------
    def _log_line(self, text: str) -> None:
        self._log.appendPlainText(text.rstrip("\n"))

    def _preview(self) -> None:
        try:
            files, _, cfg = self._targets()
        except ValueError as exc:
            QMessageBox.warning(self, "Preview", str(exc))
            return
        keep = "all" if cfg.keep_all else (
            f"{len(cfg.keep)} selected" if cfg.keep is not None
            else f"defaults ({len(DEFAULT_KEEP)})")
        lines = [f"{len(files)} file(s) -> .{cfg.fmt} @ {cfg.bitrate_kbps}kbps | tags: {keep}"]
        embed = [s for s, on in ((".lrc→lyrics", cfg.embed_lrc), (".txt→comment", cfg.embed_txt)) if on]
        if embed:
            lines[0] += f" | embed {', '.join(embed)}"
        lines[0] += f" | {int(self._jobs.value())} workers"
        try:
            info = probe(files[0])
            cover = (f"{info.cover_width}x{info.cover_height}"
                     if info.has_cover else "no cover")
            lines.append(f"first: {files[0].name} [{info.audio_codec}] {cover}")
        except Exception as exc:  # keep preview non-fatal
            lines.append(f"probe failed for first file: {exc}")
        self._log_line("\n".join(lines))

    def _run(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        try:
            files, out_dir, cfg = self._targets()
        except ValueError as exc:
            QMessageBox.warning(self, "Compress", str(exc))
            return
        self._remember(cfg, out_dir)
        src_root = Path(self._src.text().strip())
        self._worker = CompressWorker(files, src_root, out_dir, cfg,
                                      self._overwrite.isChecked(),
                                      jobs=int(self._jobs.value()))
        self._worker.message.connect(self._log_line)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished_ok.connect(self._on_finished)
        self._progress.setMaximum(len(files))
        self._progress.setValue(0)
        self._status.setText(f"0/{len(files)}")
        self._run_btn.setEnabled(False)
        self._worker.start()

    def _on_progress(self, done: int, total: int) -> None:
        self._progress.setValue(done)
        self._status.setText(f"{done}/{total}")

    def _on_finished(self, done: int, errors: int) -> None:
        self._run_btn.setEnabled(True)
        self._status.setText(f"done: {done} file(s), {errors} error(s)")
        self._worker = None


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Audiocompress")
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
