"""Completion chime (stdlib only, never raises, never loud).

Plays the bundled ``assets/chime.wav``. The file on disk is left untouched;
at load time 16-bit samples are gently scaled down if their peak exceeds
``MAX_PEAK`` (~35% full scale) so the chime stays quiet on any system
volume. The default OS ding is never used. Playback:

- Windows: winsound.PlaySound from memory (async, no temp file); if that
  fails, a temp WAV + a temp PowerShell script (System.Media.SoundPlayer)
  plays the same bytes.
- macOS/Linux: temp WAV + the first available player (afplay / paplay /
  aplay / ffplay / SoX play / mpv), in a daemon thread with a timeout so
  the UI never blocks.
- No player / headless CI: stays silent, returns False.

If the bundled asset is missing (e.g. a stripped install), falls back to a
quiet synthesized two-note motif so completion still has a distinct sound.

Usage:
    from audiocompress.chime import play_chime
    play_chime()  # fire-and-forget, safe on any thread
"""

from __future__ import annotations

import io
import math
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import wave
from importlib import resources
from pathlib import Path

# Peak ceiling as a fraction of int16 full scale. The bundled file is
# attenuated in memory to stay at/under this — never startling.
MAX_PEAK = 0.35

# Fallback synth only (used when assets/chime.wav is missing).
_VOLUME = 0.25
_SAMPLE_RATE = 22050
_NOTES: tuple[tuple[float, float], ...] = ((659.25, 0.14), (880.0, 0.22))
_GAP_SILENCE = 0.02

_cached: bytes | None = None


def _synth_wav_bytes(volume: float = _VOLUME, rate: int = _SAMPLE_RATE) -> bytes:
    """Render the fallback two-note motif to WAV bytes (16-bit mono)."""
    volume = max(0.0, min(1.0, volume))
    frames: list[float] = []
    for freq, dur in _NOTES:
        n = max(1, int(rate * dur))
        ramp = max(1, int(rate * 0.01))
        for i in range(n):
            env = 1.0
            if i < ramp:
                env = (i + 1) / ramp
            elif i >= n - ramp:
                env = (n - i) / ramp
            frames.append(volume * env * math.sin(2.0 * math.pi * freq * i / rate))
        frames.extend([0.0] * int(rate * _GAP_SILENCE))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(
            struct.pack(f"<{len(frames)}h", *(int(max(-1.0, min(1.0, s)) * 32767) for s in frames))
        )
    return buf.getvalue()


def _soften_wav(data: bytes, max_peak: float = MAX_PEAK) -> bytes:
    """Scale 16-bit WAV bytes down if their peak exceeds `max_peak`.

    Returns the input unchanged when already quiet, unreadable, or not
    16-bit — never raises.
    """
    try:
        with wave.open(io.BytesIO(data), "rb") as wf:
            params = wf.getparams()
            raw = wf.readframes(wf.getnframes())
        if params.sampwidth != 2 or not raw:
            return data
        n = len(raw) // 2
        samples = struct.unpack(f"<{n}h", raw)
        peak = max(abs(v) for v in samples)
        ceiling = max_peak * 32767
        if peak <= ceiling:
            return data
        gain = ceiling / peak
        out = struct.pack(f"<{n}h", *(int(v * gain) for v in samples))
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setparams(params)
            wf.writeframes(out)
        return buf.getvalue()
    except Exception:
        return data


def chime_wav_bytes() -> bytes:
    """Bundled chime WAV bytes (softened), cached. Never raises."""
    global _cached
    if _cached is not None:
        return _cached
    try:
        data = (resources.files(__package__) / "assets" / "chime.wav").read_bytes()
        _cached = _soften_wav(data)
    except Exception:
        try:
            _cached = _synth_wav_bytes()
        except Exception:
            _cached = b""
    return _cached


def _write_temp_wav(data: bytes) -> str:
    """Write WAV bytes to a temp file. Returns the path (caller deletes)."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as fh:
        fh.write(data)
        return fh.name


def _play_via_powershell_script(wav_path: str) -> bool:
    """Windows fallback: temp .ps1 playing our WAV via SoundPlayer.

    Same bundled sound, distinct from the OS ding; the script is deleted
    afterwards. Never raises.
    """
    ps1 = ""
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".ps1", delete=False, mode="w", encoding="utf-8"
        ) as fh:
            # -NoProfile/-NonInteractive for speed; playback is sub-second.
            fh.write(
                "(New-Object System.Media.SoundPlayer '"
                + wav_path.replace("'", "''")
                + "').PlaySync()"
            )
            ps1 = fh.name
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                ps1,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
        return True
    except Exception:
        return False
    finally:
        try:
            if ps1:
                Path(ps1).unlink(missing_ok=True)
        except Exception:
            pass


def _play_sync(data: bytes) -> bool:
    """Blocking playback of the bundled chime. True if sound was emitted."""
    if not data:
        return False
    if sys.platform == "win32":
        try:
            import winsound

            winsound.PlaySound(
                data, winsound.SND_MEMORY | winsound.SND_ASYNC | winsound.SND_NODEFAULT
            )
            return True
        except Exception:
            pass
        # Distinct fallback: same WAV via a temp PowerShell script.
        # (Deliberately no MessageBeep/Beep here — those are the OS ding.)
        if shutil.which("powershell"):
            tmp = ""
            try:
                tmp = _write_temp_wav(data)
                return _play_via_powershell_script(tmp)
            except Exception:
                return False
            finally:
                try:
                    if tmp:
                        Path(tmp).unlink(missing_ok=True)
                except Exception:
                    pass
        return False
    # macOS / Linux: first available CLI player on a temp WAV file.
    # ffplay/mpv need extra flags; plain players just take the path.
    candidates: tuple[list[str], ...]
    if sys.platform == "darwin":
        candidates = (
            ["afplay"],
            ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"],
            ["play"],
            ["mpv", "--no-video", "--really-quiet"],
            ["paplay"],
            ["aplay", "-q"],
        )
    else:
        candidates = (
            ["paplay"],
            ["aplay", "-q"],
            ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"],
            ["play", "-q"],
            ["mpv", "--no-video", "--really-quiet"],
            ["afplay"],
        )
    player: list[str] | None = None
    for candidate in candidates:
        if shutil.which(candidate[0]):
            player = candidate
            break
    if player is None:
        return False
    tmp = ""
    try:
        tmp = _write_temp_wav(data)
        subprocess.run(
            [*player, tmp],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
        return True
    except Exception:
        return False
    finally:
        try:
            if tmp:
                Path(tmp).unlink(missing_ok=True)
        except Exception:
            pass


def play_chime() -> bool:
    """Fire-and-forget bundled chime. Never raises; True if playback started.

    Windows PlaySound is already async; on other platforms playback runs in
    a daemon thread so the UI thread never blocks on `aplay`-style players.
    """
    try:
        data = chime_wav_bytes()
    except Exception:
        return False
    if sys.platform == "win32":
        try:
            return _play_sync(data)
        except Exception:
            return False
    # Non-blocking elsewhere: daemon thread, errors swallowed.

    def _bg() -> None:
        try:
            _play_sync(data)
        except Exception:
            pass

    try:
        threading.Thread(target=_bg, name="audiocompress-chime", daemon=True).start()
        return True
    except Exception:
        return False


__all__ = ["MAX_PEAK", "chime_wav_bytes", "play_chime"]
