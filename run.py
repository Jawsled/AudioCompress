"""Bootstrap launcher: ensures an isolated project venv exists, then runs CLI.

Cross-platform (Windows/macOS/Linux, stdlib only). Usage:
    python run.py batch ./in ./out --cover-size 800
    python run.py file in.flac out.opus
    python run.py gui
With no arguments, defaults to `--help`. The venv is created and installed
automatically on first run; `gui` additionally ensures PySide6 is available.
"""

from __future__ import annotations

import os
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_DIR = ROOT / ".venv"


def _venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def ensure_venv() -> Path:
    py = _venv_python()
    if not py.exists():
        print(f"[bootstrap] creating isolated venv at {VENV_DIR} ...")
        venv.create(VENV_DIR, with_pip=True)
    check = subprocess.run(
        [str(py), "-c", "import audiocompress"],
        capture_output=True,
        cwd=ROOT,
    )
    marker = VENV_DIR / ".audiocompress-installed"
    need_install = check.returncode != 0
    if not need_install and marker.exists():
        try:
            need_install = (
                (ROOT / "pyproject.toml").stat().st_mtime > marker.stat().st_mtime
            )
        except OSError:
            need_install = True
    if need_install:
        print("[bootstrap] installing audiocompress + pinned deps into .venv ...")
        subprocess.run(
            [str(py), "-m", "pip", "install", "--upgrade", "pip"],
            check=True,
            cwd=ROOT,
        )
        subprocess.run(
            [str(py), "-m", "pip", "install", "-e", "."],
            check=True,
            cwd=ROOT,
        )
        marker.touch()
    return py


def _ensure_qt(py: Path) -> bool:
    if subprocess.run(
        [str(py), "-c", "import PySide6"],
        capture_output=True,
        cwd=ROOT,
    ).returncode == 0:
        return True
    print("[bootstrap] PySide6 not installed, installing ...")
    proc = subprocess.run(
        [str(py), "-m", "pip", "install", "PySide6"],
        cwd=ROOT,
    )
    return proc.returncode == 0


def _gui_module(py: Path) -> str:
    if _ensure_qt(py):
        return "audiocompress.gui_qt"
    print(
        "[bootstrap] PySide6 install failed, falling back to tkinter GUI."
    )
    return "audiocompress.gui"


def main(argv: list[str]) -> int:
    py = ensure_venv()
    if not argv:
        argv = ["--help"]
    if argv[0] == "gui":
        module = _gui_module(py)
        proc = subprocess.run([str(py), "-m", module], cwd=ROOT)
        return proc.returncode
    proc = subprocess.run([str(py), "-m", "audiocompress.cli", *argv], cwd=ROOT)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
