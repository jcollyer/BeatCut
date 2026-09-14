# BeatCut

A desktop tool that takes your music + a pile of video clips, cuts the clips to
the beat, and exports a **Final Cut Pro 7 XML** (`.xml` / xmeml) rough cut you
finish in Premiere Pro. Nothing is rendered or copied — the XML just points at
your media by file path, so it's tiny and fast.

> **Early prototype.** It builds and runs with Tauri 2 + madmom, and its exports
> import into Premiere Pro (checked at 30 and 25 fps). Only tested on macOS
> (Apple Silicon). [SPEC.md](SPEC.md) is the authoritative spec and phased build
> plan; Phases 0 (build and run) and 1 (frame rate and size from clips) are
> done. The core logic (`timeline.rs` + `fcp7xml.rs`) is validated against a
> Python mirror in `spec/`.

## The model (clip-driven)

* Video track is the **master and always contiguous** — never a gap.
* Songs play in order; song 0 starts at t=0.
* Clips are shuffled (seeded — re-roll = new seed) and used **once**, laid
  left-to-right. Each clip keeps its full length, but its **end trims back to
  the nearest beat at or before its natural end** (least trim; the next clip
  then starts on a beat).
* If a clip's end runs past the current song, it **overruns**: plays full
  length, and the next song is **pushed** to start at the clip's end — opening a
  silent gap on the *audio* track (allowed). A final overrunning clip plays out
  over silence, then we stop.
* Stop when clips or songs run out. Leftover clips are reported as unused.
* Clip **duration is required** (ffprobe or another source) — we can't place a
  clip without knowing its length.

## How it fits together

```
 ┌── frontend (Vite, vanilla JS) ──────────────┐
 │  add audio  →  invoke detect_beats_cmd       │
 │  add clips  →  invoke probe_duration         │
 │  Generate   →  invoke generate_timeline      │  ← preview slots
 │  Export     →  invoke export_xml             │  ← writes .xml
 └───────────────┬──────────────────────────────┘
                 │ Tauri IPC
 ┌───────────────▼──────────── Rust (src-tauri/src) ─────────────┐
 │  beats.rs     runs the Python sidecar, parses beats/tempo      │
 │  timeline.rs  THE CORE: beats → frame-snapped video slots      │
 │  fcp7xml.rs   renders a Timeline as xmeml Premiere can import   │
 └───────────────┬──────────────────────────────────────────────┘
                 │ std::process::Command
 ┌───────────────▼── python/detect_beats.py (madmom) ────────────┐
 │  RNNBeatProcessor + DBNBeatTrackingProcessor → beat times       │
 └───────────────────────────────────────────────────────────────┘
```

The two files worth reading first: **`src-tauri/src/timeline.rs`** (the
slot-filling / randomization / frame-snapping logic) and
**`src-tauri/src/fcp7xml.rs`** (what actually gets written for Premiere).

## Prerequisites

- **Rust** (stable) + the [Tauri 2 prerequisites](https://v2.tauri.app/start/prerequisites/)
  for your OS (on macOS, the Xcode Command Line Tools).
- **Node 20.19+ or 22.12+** (required by Vite 8)
- **ffmpeg + ffprobe** on PATH — **required**. ffprobe measures clip durations
  (clips it can't measure are dropped), and madmom uses ffmpeg to decode
  non-WAV songs. On macOS: `brew install ffmpeg`.
- **Python 3.10–3.12** (3.12 tested) in a venv with **madmom**. Don't install
  madmom from PyPI: its latest release (0.16.1, from 2018) breaks on current
  Python/NumPy. From the repo root, install a pinned GitHub commit instead:
  ```bash
  python3.12 -m venv .venv
  .venv/bin/pip install "numpy<2" "cython<3.1" setuptools wheel pytest-runner
  .venv/bin/pip install --no-build-isolation "numpy<2" \
    "madmom @ git+https://github.com/CPJKU/madmom@27f032e8947204902c675e5e341a3faf5dc86dae"
  ```
  `--no-build-isolation` compiles madmom against the venv's `numpy<2`. On macOS,
  `brew install python@3.12` provides `python3.12`.

## Run it (dev)

```bash
npm install
BEATCUT_PYTHON="$PWD/.venv/bin/python" npm run tauri dev
```

The first run compiles the Rust side, which takes a few minutes.
`BEATCUT_PYTHON` should be the absolute path to the venv's interpreter; if it's
unset the app falls back to `python3`, which usually has no madmom, so every
song shows "error".

In the app: **Add music** (wav, mp3, aif/aiff, flac, m4a, mp4) → **Add clips**
(mp4, mov, mkv, m4v, avi, mxf) → **Generate cut** → **Export for Premiere**.
In Premiere, use File ▸ Import on the exported `.xml`.

**Troubleshooting**
- *A song shows "error":* run the detector outside the app —
  `.venv/bin/python src-tauri/python/detect_beats.py path/to/song.mp3` should
  print JSON with `beats`, `tempo` and `duration`.
- *A clip shows "—" instead of a duration:* usually ffprobe isn't on PATH in
  the shell that ran `npm run tauri dev`.
- *"Port 1420 is already in use":* another dev server is still running.

## What works vs. what's next

**Working now**
- Add multiple songs; beats detected per song via madmom
- Add clips; ffprobe measures duration, frame rate and size. Files without a
  video stream (e.g. a song added as a clip) are flagged and left out
- Remove any added song or clip (×)
- "Auto" frame rate and resolution from the first clip, with manual overrides
  (presets or a custom size)
- Generate: seeded shuffle, clip ends trimmed back to a beat, contiguous video,
  overruns push the next song, optional max clip length; re-roll = new seed
- Timeline preview (audio blocks + beat ticks, video slots)
- Export valid xmeml with per-file dedup; imports into Premiere Pro

**Not done yet** (phases from [SPEC.md](SPEC.md) §9)
- **Golden test in Rust** (Phase 2).
- **Preview + light edits** — lock, replace one clip, thumbnails (Phase 3).
- **Robustness** (Phase 4): NTSC round-trip validation (23.976/29.97 set the
  ntsc flag but aren't validated), cross-OS `file://` URLs (see `to_file_url` in
  fcp7xml.rs), media characteristics in `<file>` to cut relink prompts, and
  bundling Python as a frozen sidecar so users don't need a venv.
- **Packaging** (Phase 5): the app icons are placeholders; no signed builds yet.
- Also open, not yet planned: stereo / multi-channel audio (currently one audio
  clipitem per file on a single track), downbeat-aware cuts, drag-to-reorder,
  zoom, waveform rendering.

## Settled spec

- Clip-driven placement, contiguous video, seeded re-roll, each clip once.
- End trims to nearest beat at/before natural end (beats only, not bars).
- Overrun pushes next song; silent audio gaps allowed; last clip plays out.
- Auto fps/res from first clip (override available); optional max-clip cap;
  hard cuts; unused clips reported by count + name.
