<<<<<<< HEAD
# AudioCompress
Compress high-bitrate audio (FLAC/WAV/ALAC…) to manageable Opus/Ogg/MP3 files with tag selection and not ruining the cover art quality.
=======
# Audiocompress

Compress high-bitrate audio (FLAC/WAV/ALAC…) to manageable Opus/Ogg/MP3
files with tag selection and not ruining the cover art quality.

## Why compress the perfectly good High-bitrate audio?

Like everyone, I enjoy listening to music all the time - and as  great as high-res audio are, the size is  always a big catch. If I'm listening to music while I'm in a car going somehwere or just out and  about, I don't need higheset quality audio, and my phone / DAP storage agree. Compressing the  audio  file that you arealdy have is a great way to reduce storage space that it occupies on your device, especially on space-constrained situation like on a mobile devices.

## Is this yet another ffmpeg wrapper?

Short answer - yes. it uses ffmpeg as its core component.
However, this program does what normal / plain ffmpeg is (as far as I understand) not able to.

ffmpeg re-encodes or drops the cover when targeting Opus/Ogg (cover goes from
an attached picture stream to a base64 `METADATA_BLOCK_PICTURE` comment), and
`-map_metadata` copies incompatible/bloated tags.

## How it works

1. **Probe:** streams/tags/cover (ffprobe + mutagen, read-only).
2. **Extract:** tags + cover bytes losslessly (mutagen, no re-encode).
3. **Transcode audio-only:** (`-map 0:a:0 -vn -map_metadata -1`) so the image
   can't be degraded.
4. **Resize cover with Pillow only if oversized:** (Lanczos, JPEG q93);
   otherwise re-embed byte-exact.
5. **Re-embed:** filtered tags + cover, then verify sizes.

## Quick start

```
git clone https://github.com/Jawsled/AudioCompress.git
cd AudioCompress

python audiocompress.py batch --help   # creates venv on the first run
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```
**Note:** Requires `ffmpeg`/`ffprobe` on PATH. otherwise the bundled
`imageio-ffmpeg` binary is used for transcoding.


## Usage (CLI)

```
python audiocompress.py file in.flac out.opus --cover-size 800
python audiocompress.py batch .\in .\out --format opus --bitrate 160 --cover-size 500
python audiocompress.py batch .\in .\out --format mp3 --bitrate 320
python audiocompress.py batch .\in .\out --format ogg --dry-run
python audiocompress.py tags .\in   # show which tags your files have
```

Defaults: `--format opus`, `--bitrate 160` (32–320), `--cover-size 1000`
(presets `0=original, 500, 600, 800, 1000`), `--cover-quality 93`.
Tags: default whitelist (title/artist/album/…) dropping encoder/cuesheet/
replaygain; override with `--keep-all`, `--keep title,artist`,
`--drop comment`. Batch mirrors the input tree and skips existing outputs
unless `--overwrite`.

## GUI

```powershell
pip install -e ".[dev,gui]"   # adds PySide6 for the Qt GUI
python audiocompress.py gui       # Qt GUI when installed, tkinter fallback otherwise
```

No extra build step: pick a source file/folder, a destination
folder, set format/bitrate/cover preset, then Preview or Compress. **Select**
opens the tag picker (friendly names + example values from your files, with
a search filter in the Qt version).
Batch runs in a background thread with a progress bar and per-file log
(same line format as the CLI).
Settings are remembered between runs.

## Tags

`tags <file-or-folder>` (or the GUI's **Select** button) shows every tag key
found in your files with a friendly name and an example value, plus whether
it is kept by default — so you don't need to know raw tag names. MP3 inputs
(ID3 frames like TIT2/TPE1) are normalized to the same names.

## Settings

Last-used options (format, bitrate, cover size/quality, tag selection,
destination folder) are remembered in `~/.audiocompress/config.json` and
pre-filled next time, in both CLI and GUI. No setup needed.

## Cover quality vs size

Included for your convinience to get better idea on which resolution and quality you might want to use.
For measurement, I used 1000x1000 px cover image from my own collection. Your result may vary.

Graphed change for visual purpose. (Lanczos and re-encoded with `optimize=True`)

| q | 1000px | 800px | 600px | 500px |
|---:|---:|---:|---:|---:|
| 70  | 261 KB (-65%) | 180 KB (-76%) | 112 KB (-85%) | 82 KB (-89%) |
| 80  | 327 KB (-56%) | 218 KB (-71%) | 135 KB (-82%) | 99 KB (-87%) |
| 85  | 381 KB (-49%) | 247 KB (-67%) | 153 KB (-79%) | 112 KB (-85%) |
| 90  | 430 KB (-42%) | 293 KB (-61%) | 180 KB (-76%) | 132 KB (-82%) |
| 93  | 464 KB (-38%) | 334 KB (-55%) | 205 KB (-72%) | 150 KB (-80%) |
| 95  | 503 KB (-32%) | 378 KB (-49%) | 232 KB (-69%) | 170 KB (-77%) |
| 98  | 586 KB (-21%) | 480 KB (-35%) | 292 KB (-61%) | 214 KB (-71%) |
| 100 | 744 KB (-00%)  | 599 KB (-19%) | 362 KB (-51%) | 263 KB (-65%) |

Going from 1000px to 500px is a great way to reduce size, especially if you are intending to use it on mobile devices. This reduces the size by ~68%.
As for quality preset, I would recommend not going below 90%. I find 93 or 95 % to be the sweet spot with no real visible quality drop.

## Audio output and size table

`ffmpeg
-vn -map_metadata -1 -map 0:a:0` (the same flags `audiocompress` uses), so
the numbers match what the tool actually produces. Multiply `KB / min`
by your track length in minutes to estimate any file.

The test source was a 2:22 FLAC track. (16.31 MB, ~961 kbps average).

### OPUS

| preset | KB / min | size of this track | ratio vs FLAC |
|---|---:|---:|---:|
| 64 kbps  | 473 KB  | 1.10 MB | 14.9x smaller |
| 96 kbps  | 708 KB  | 1.64 MB | 9.9x smaller |
| 128 kbps | 945 KB  | 2.19 MB | 7.4x smaller |
| 160 kbps | 1180 KB | 2.73 MB | 6.0x smaller |
| 192 kbps | 1414 KB | 3.28 MB | 5.0x smaller |
| 256 kbps | 1886 KB | 4.37 MB | 3.7x smaller |
| 320 kbps | N/A     | N/A     | N/A |

Opus 160Kbps is the default preset, which is in my humble opinion, is a sweet spot for size and good enough quality for just outdoor listening, comperable to Spotify's standard quality.
Opus is  meant for compression and does not support 320Kbps.

### OGG (Vorbis)

| preset | KB / min | size of this track | ratio vs FLAC |
|---|---:|---:|---:|
| 96 kbps  | 671 KB  | 1.56 MB | 10.5x smaller |
| 128 kbps | 919 KB  | 2.13 MB | 7.7x smaller |
| 160 kbps | 1140 KB | 2.64 MB | 6.2x smaller |
| 192 kbps | 1359 KB | 3.15 MB | 5.2x smaller |
| 256 kbps | 1939 KB | 4.49 MB | 3.6x smaller |
| 320 kbps | 2539 KB | 5.88 MB | 2.8x smaller |

Vorbis needs ~30% more bitrate than Opus for similar quality.
Pick OGG only if you need a player that doesn't support Opus.

### MP3

| preset | KB / min | size of this track | ratio vs FLAC |
|---|---:|---:|---:|
| 128 kbps | 938 KB  | 2.17 MB | 7.5x smaller |
| 160 kbps | 1172 KB | 2.72 MB | 6.0x smaller |
| 192 kbps | 1407 KB | 3.26 MB | 5.0x smaller |
| 256 kbps | 1876 KB | 4.35 MB | 3.8x smaller |
| 320 kbps | 2345 KB | 5.43 MB | 3.0x smaller |

MP3 needs ~100% more bitrate than Opus, ~40% more than Vorbis OGG.
Pick it only if you must.

## Layout

- `audiocompress.py` — venv auto-setup + CLI launcher
- `src/audiocompress/` — `probe`, `extract`, `transcode`, `cover`, `remux`,
  `pipeline`, `cli`, `gui`, `ffmpeg_bin`, `settings`, `metadata_map`
- `tests/` — cover presets, tag filtering, end-to-end FLAC→Opus (`pytest`)
>>>>>>> bdad32d (innitial commit)
