# S-Clip

A lightweight Windows screen recorder with a true rolling replay buffer.

![status](https://img.shields.io/badge/status-beta-blue)

## What it does

S-Clip is a desktop application that records your screen on Windows. It provides three
ways to capture footage:

- **Rolling replay buffer.** S-Clip keeps a continuous recording of the last thirty
  seconds (configurable) running quietly in the background. When something noteworthy
  happens, you press the clip hotkey and S-Clip stitches the buffer into a finished,
  perfectly smooth MP4 — ready in a couple of seconds.
- **Manual recording.** Press the record hotkey to start a full recording, press it
  again to stop. The resulting file lands in your configured clips directory.
- **Clip library.** A built-in panel lists every clip you have produced, lets you
  preview it, rename it or reveal it in Explorer.

Every recording can carry both the **game and desktop sound** and your **microphone**,
mixed into one track. Desktop audio is captured through the Windows WASAPI loopback
API — the same approach OBS and Medal use — so it works out of the box with no
"Stereo Mix" device and no virtual audio cable to install.

S-Clip also **configures itself on first launch**: it detects the best hardware video
encoder, the primary display and its resolution, and tunes the capture settings
accordingly. An Advanced mode in Settings is there if you want to take the wheel.

## Why it exists

The previous version of S-Clip advertised an "instant replay" feature, but the
implementation actually started a fresh recording when you pressed the hotkey. That
meant you got the *next* thirty seconds, not the *previous* thirty — exactly the
opposite of what users wanted. The rewrite uses FFmpeg's segment muxer to maintain
a genuine rolling buffer on disk, so the clip really is "what just happened".

The old build also captured the screen with FFmpeg's `gdigrab`, a GDI screen-scrape
that could not keep pace at higher resolutions and frame rates — it delivered frames
late and unevenly, so every saved clip juddered. The rewrite captures through
`ddagrab` instead, the Windows Desktop Duplication path: it runs on the GPU, hands
frames back at the true refresh rate, and produces genuinely constant-rate video.
Saved clips are now smooth.

The rewrite also tidies up a number of long-standing rough edges: the settings file
is now atomic on save and tolerant of legacy schemas on load; the device-listing
code no longer flashes a console window every time the settings dialog opens; the
GUI and the capture engine are properly decoupled behind small, typed protocols.

## Installation

S-Clip targets Windows 10 and 11 with Python 3.10 or newer.

### For users

```
pip install .
```

Then launch the application with `sclip` (console) or `sclip-gui` (no console window).

### For development

```
pip install -e ".[dev]"
```

The `dev` extra pulls in pytest, pytest-qt, pytest-cov, ruff and mypy.

### FFmpeg requirement

S-Clip needs an FFmpeg binary at runtime. Two options are supported:

- **Bundled FFmpeg.** Drop an `ffmpeg-7.1-essentials_build/bin/ffmpeg.exe` into the
  project root (or the package root if you have installed S-Clip as a wheel). The
  application prefers this copy over anything else.
- **System FFmpeg.** Install FFmpeg 4.0 or newer and make sure `ffmpeg` is on your
  `PATH`. S-Clip falls back to this if it cannot find the bundled binary.

GPU encoding through NVENC, AMF or Quick Sync is detected automatically. NVENC in
particular requires an NVIDIA driver from approximately the last three years —
older drivers do not expose the FFmpeg encoder names S-Clip selects.

## Usage

### Hotkeys

| Hotkey               | Action                                    |
|----------------------|-------------------------------------------|
| F5 (default)         | Save the last *n* seconds as a clip       |
| Ctrl+F6 (default)    | Toggle manual recording on or off         |

Both hotkeys are remappable in the settings dialog. Hotkeys are global — they fire
even when S-Clip is minimised or hidden behind a full-screen application.

### Manual recording

Open the application, choose your monitor, audio inputs and quality preset, and
press the record hotkey or click the "Record" button. The capture writes directly
to your clips directory with a timestamped filename. Press the hotkey again (or the
"Stop" button) to finish; FFmpeg writes the MP4 trailer cleanly so the file is
playable straight away.

### Replay buffer

Enable the replay buffer in the settings page and choose how many seconds it
should hold (default thirty). The application keeps a continuous capture running
in the background, rotating short two-second segments through a temp directory.
When you press the clip hotkey, S-Clip stitches the buffered segments into a
single MP4.

That final stitch re-encodes the footage, and the choice is deliberate. Copying
the segments verbatim would leave a faint timing seam at every join, because each
segment's audio track makes its container a hair longer than its video.
Re-encoding lays down one unbroken, constant-rate timeline, so the saved clip
plays back perfectly smoothly. A thirty-second clip takes a few seconds to write.

The replay buffer uses a small amount of disk per second of buffered video — at
1080p60 with a sensible quality preset, expect roughly twenty megabytes for a
thirty-second window. The buffer's temporary segments are cleaned up when the
buffer stops or the application exits.

## Configuration

Settings live in `%APPDATA%\S-Clip\settings.json` on Windows. The file is created
on first save; until then, S-Clip uses the defaults below.

| Field             | Type     | Default      | Description                                            |
|-------------------|----------|--------------|--------------------------------------------------------|
| `resolution`      | string   | `1920x1080`  | Output resolution as `WIDTHxHEIGHT`                    |
| `fps`             | integer  | `60`         | Capture frame rate (clamped to 1-240)                  |
| `encoder`         | string   | `libx264`    | One of `libx264`, `h264_nvenc`, `hevc_nvenc`, `h264_amf`, `h264_qsv` |
| `preset`          | string   | `veryfast`   | Encoder-specific speed preset                          |
| `crf`             | integer  | `20`         | Constant quality value; lower is better (0-51)         |
| `audio_input`     | string   | `""`         | DirectShow name of the microphone; blank means no microphone |
| `capture_audio`   | boolean  | `true`       | Master switch for all audio capture                    |
| `capture_desktop_audio` | boolean | `true`  | Capture system/game sound via WASAPI loopback          |
| `replay_buffer`   | boolean  | `true`       | Whether the rolling buffer runs                        |
| `replay_seconds`  | integer  | `30`         | Length of the rolling buffer (5-600)                   |
| `monitor`         | string   | `Monitor 1`  | Which display to capture                               |
| `clip_hotkey`     | object   | `{key:"F5"}` | Hotkey to save a replay clip                           |
| `record_hotkey`   | object   | `{key:"F6", ctrl:true}` | Hotkey to toggle manual recording           |
| `output_dir`      | string   | `""`         | Override clip directory; blank uses the platform default |

A complete example is bundled as [`settings.json.example`](./settings.json.example).

## Architecture

S-Clip is split into three layers: small typed contracts in `sclip.contracts`,
the I/O and FFmpeg machinery in `sclip.core`, and the PySide6 user interface in
`sclip.ui`. Subsystems communicate through plain callbacks rather than Qt signals
so the core is testable without a `QApplication`.

See [`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md) for module diagrams,
responsibility tables, threading boundaries and design decisions.

## Development

Run the fast unit tests:

```
pytest -m "not slow"
```

Run everything, including the slow integration tests that exercise the rolling
buffer:

```
pytest
```

Lint and format the source tree:

```
ruff check .
ruff format .
```

Static type-check the package:

```
mypy src
```

## Licence

S-Clip is released under the MIT licence. See [`LICENSE`](./LICENSE) for the full text.
