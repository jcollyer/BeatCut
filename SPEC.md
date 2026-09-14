# BeatCut — Build Spec & Hand-off

Hand-off document for continuing this project in Claude Code. It is the
authoritative description of the agreed behaviour. Where this document and the
current code disagree, **this document wins** — fix the code.

---

## 1. What we're building

A desktop app that automates a beat-synced drone-footage montage. The user
drops in several songs and many video clips; the app shuffles the clips, lays
them end-to-end over the songs, trims each clip's end to a nearby beat so cuts
land musically, and exports a **Final Cut Pro 7 XML (xmeml)** rough cut that the
user finishes in Adobe Premiere Pro. The app never renders or copies media — the
XML only references files by path, so it's fast and light.

Primary loop: **add songs → add clips → Generate → preview → re-roll until happy
→ Export → finish in Premiere.**

---

## 2. Current status (what's in this repo)

**Phases 0 and 1 are done** (2026-09-14): the app builds and runs with
`npm run tauri dev` (tested on macOS), and its exports import into Premiere.

Validated:
- The core algorithm (`src-tauri/src/timeline.rs`) and the XML renderer
  (`src-tauri/src/fcp7xml.rs`) are mirrored in `spec/golden_reference.py`, run on
  fixed sample data, and produce **well-formed xmeml** that imports into Premiere
  cleanly. See `spec/beatcut-sample.golden.xml` and §5.4.
- On real exports (7 songs, ~60 clips, 25 and 30 fps) the app's XML is
  byte-identical to the Python mirror's for the same clip order, except where the
  mirror's `round()` breaks exact half-frame ties differently from Rust.
- "Auto" frame rate and resolution come from the first clip via ffprobe
  (rotation applied, cover art ignored), with manual overrides for both.

Not yet done / unverified:
- No Rust tests yet (Phase 2).
- No preview or light-edit UI beyond a static timeline render and removing
  added files.
- Python sidecar assumes a local interpreter with madmom (not bundled).
- Only tested on macOS; NTSC rates aren't validated in Premiere (Phase 4).

---

## 3. Tech stack & key constraints

- **Shell:** Tauri 2 (Rust backend + webview frontend).
- **Frontend:** Vite 8 + vanilla JS (no framework). Keep it dependency-light.
- **Backend:** Rust. Crates: `tauri` 2, `tauri-plugin-dialog` 2, `serde`,
  `serde_json`, `rand` 0.8.
- **Beat detection:** Python **madmom** sidecar, invoked via
  `std::process::Command`. Script is embedded with `include_str!` and written to
  a temp file at startup, so it doesn't depend on the working directory.
- **Media probing:** **ffprobe** (from ffmpeg) — **required** (see §5). Reports
  each clip's duration, frame rate and display size (`probe_media`, §6).
- **Interchange format:** **FCP7 XML / xmeml v5**, extension `.xml`. This is what
  Premiere imports (File ▸ Import). Do **not** target `.fcpxml` — that is modern
  Final Cut and Premiere cannot read it. The two are incompatible despite the
  shared "Final Cut XML" name.

> ⚠️ Version check: exact Tauri 2 plugin/permission strings and the capabilities
> schema move between minor versions. Verify against current Tauri docs when
> standing up Phase 0 rather than trusting the committed config verbatim.

---

## 4. Repository layout

```
beatcut/
├── SPEC.md                         ← this file
├── README.md                       ← setup / run instructions
├── index.html                      ← UI structure
├── package.json / vite.config.js
├── src/
│   ├── main.js                     ← file picking, IPC, timeline render
│   └── styles.css                  ← dark editing-console theme
├── src-tauri/
│   ├── Cargo.toml / build.rs / tauri.conf.json
│   ├── capabilities/default.json
│   ├── python/detect_beats.py      ← madmom sidecar
│   └── src/
│       ├── main.rs                 ← Tauri commands
│       ├── beats.rs                ← run sidecar + ffprobe
│       ├── timeline.rs             ← CORE placement algorithm
│       └── fcp7xml.rs              ← xmeml renderer
└── spec/
    ├── golden_reference.py         ← Python mirror = test oracle
    └── beatcut-sample.golden.xml   ← validated expected output
```

The two files to protect the behaviour of: **`timeline.rs`** (placement) and
**`fcp7xml.rs`** (export). Everything else serves them.

---

## 5. The core model (authoritative)

### 5.1 Definitions

- **Timeline** runs left to right in seconds, snapped to whole frames on output.
- **Song**: an audio file with beats (seconds, relative to the song's own start)
  and a duration. Songs play in program order; song 0 starts at t = 0.
- **Clip**: a video file with a known duration. Each clip is used **at most
  once**.
- **Natural end** of a clip placed at time `t` = `t + effective_length`, where
  effective length is the clip's duration, optionally capped (§5.2).

### 5.2 Algorithm

1. **Shuffle** clip order with a seeded RNG. A "re-roll" is simply a new seed.
2. Enter song 0 at `song_start = 0`. Record its audio item. Set `t = 0`.
3. For each clip in shuffled order, while songs remain:
   a. **Contiguous advance:** if `t >= current_song_end`, advance to the next
      song with `song_start = t` (no gap) and record its audio item. If there is
      no next song, **stop**.
   b. `effective_length = min(duration, maxClipSecs)` if a cap is set, else
      `duration`. `natural_end = t + effective_length`.
   c. **If `natural_end <= current_song_end`** (ends inside the song): the
      candidates are the beats `> t` and `<= natural_end`, and the cut mode
      picks one:
      - **Last beat** (`lastBeat`, the backend default): the **largest**
        candidate (least possible trim, lands on a beat).
      - **Strong hits** (`strongHits`, the UI default): among the last **8**
        candidates (two bars of 4/4), the highest `strength − 0.1 × beats_back`,
        with `beats_back` counted from the latest candidate. Scores within 1e-9
        tie, and ties go to the later beat. `strength` is the song analysis's
        per-beat score (§6); a song without one value per beat uses last beat.

      If there are no candidates (clip shorter than the gap to the next beat),
      use `natural_end` unsnapped.
   d. **Else (overrun):** the clip plays its **full** length (unsnapped — there
      are no beats in the silence it runs into). `clip_end = natural_end`.
   e. Emit a video slot `[frame(t), frame(clip_end)]`, `in = 0`,
      `out = length`. Set `t = clip_end`.
   f. **If it overran:** push the next song to start at `clip_end` (this opens a
      silent gap on the audio track) and record its audio item. If it was the
      **last** song, **stop** (the final clip plays out over silence).
4. **Stop** when clips run out or songs run out. Any clips not placed are
   reported as **unused** (by filename).

**Frame snapping:** `frame(sec) = round(sec * rate)`, with `rate = fps` for an
integer timebase and `fps × 1000/1001` when `ntsc` is set (an NTSC "30" is
29.97 fps; counting it at 30 would put cuts a frame late every ~33 s). Video
stays contiguous because each clip's end time is literally the next clip's start
time — the same float rounds identically, so `frame(end_i) == frame(start_{i+1})`.

### 5.3 Edge cases (all confirmed with the user)

| Situation | Behaviour |
|---|---|
| Video track | **Always contiguous — never a gap between clips.** |
| Audio track | Gaps (silence) between songs are **allowed**. |
| Clip ends inside a song | End trims to a beat **at or before** natural end: the nearest one (last beat) or the best-scoring of the last 8 (strong hits). |
| Snap granularity | **Beats only.** Strong hits favours kicks, the 1 of a bar and the kick that opens a 4-bar loop, but never looks back more than 8 beats. |
| Clip overruns a song | Plays **full length, unsnapped**; next song pushed to clip end. |
| Overrun on the **last** song | Plays out over silence, then stop. |
| Clip shorter than one beat gap | Use natural end unsnapped (rare for 7–60 s clips). |
| Sub-frame clip (`frame(end) <= frame(start)`) | Skip it; counts as unused. |
| More clips than fit | Extra clips ignored; report **count + filenames**. |
| Fewer clips than audio | Stop when clips run out (trailing audio has no video). |
| In-point | Always 0 — clips play from their start. |
| Transitions | Hard cuts only. |
| Max clip length | Optional cap; off by default (keep clips full). |

### 5.4 Worked example (the golden fixture)

Sample data: 2 songs (A = 12.0 s, B = 10.0 s, beats every 0.5 s), 8 clips, fps
30. Fixed (non-random) clip order for determinism. This is exactly
`spec/beatcut-sample.golden.xml`.

```
VIDEO (contiguous, frames @30):
    0 -> 90    cliff_pan.mp4       (3.3s trimmed to beat 3.0s)
   90 -> 255   coastline.mp4       (natural 8.8s -> beat 8.5s)
  255 -> 315   forest rise.mp4     (natural 10.7s -> beat 10.5s)  [name has a space]
  315 -> 450   river_bend.mp4      (natural 15.0s > song A end 12.0s -> OVERRUN, full)
  450 -> 540   city_dusk.mp4       (in song B; natural 18.1s -> beat 18.0s)
  540 -> 735   mountain_ridge.mp4  (natural 24.7s -> beat 24.5s)
  735 -> 885   desert_dunes.mp4    (natural 29.5s > song B end 25.0s -> OVERRUN last, plays out)
AUDIO (gap allowed):
    0 -> 360   track_A.wav
  450 -> 750   track_B.wav         (pushed from 360 by river_bend's overrun -> silence 360..450)
UNUSED: harbor_lights.mp4          (audio ran out)
TOTAL: 885 frames (29.5 s)
```

**The Rust output must match `beatcut-sample.golden.xml` byte-for-byte** when fed
this data with shuffling disabled. Regenerate the golden file with
`python3 spec/golden_reference.py`.

---

## 6. Data contracts (frontend ⇄ backend)

Tauri maps top-level command argument names camelCase (JS) → snake_case (Rust)
automatically. Struct **fields** are plain serde: `GenerateSettings` uses
`#[serde(rename_all = "camelCase")]` so JS sends camelCase; all other structs
use snake_case field names on both sides. All time values in the `Timeline`
output are **integer frames**; all inputs are **seconds (f64)**.

### Commands (`src-tauri/src/main.rs`)

| Command | JS args | Returns |
|---|---|---|
| `detect_beats_cmd` | `{ audioPath }` | `BeatResult { beats:[f64], tempo:f64?, duration:f64?, strength:[f64] }` |
| `probe_media` | `{ path }` | `MediaInfo { duration:f64?, fps:f64?, width:u32?, height:u32? }` / error string |
| `generate_timeline` | `{ songs, clips, settings }` | `Timeline` |
| `export_xml` | `{ timeline, outPath, sequenceName }` | `()` / error string |

`MediaInfo` comes from ffprobe (it replaced `probe_duration` in Phase 1). `fps`
is the measured rate (e.g. 29.97002997); `width`/`height` are the display size,
with rotation metadata applied; the video fields are null for files without a
video stream. Errors are user-facing text — a missing ffprobe says how to
install it. The frontend snaps `fps` to a standard sequence rate for "Auto".

`BeatResult.strength` holds one score per beat (empty if that analysis failed),
computed in `detect_beats.py` as `max(kick, 0.6 × snare)`, plus `0.25 × kick` on
the 1 of a bar and `0.5 × min(1, kick / 0.6)` on the 1 that opens a 4-bar loop,
so scores run from 0 to 1.75. Kick and snare are percussive onset strength at the
beat, normalized per song to 0–1. Any clear kick earns the whole loop bonus,
because the fill just before a loop start makes that kick measure weaker than it
sounds. Bars come from madmom's bar tracker run on the detected beats. Loop
starts: each downbeat gets a fill score (full-mix onset strength over the half
bar before it); when at least 70% of the song's biggest fills (its top 15% of
downbeats) sit at one place in the 4-bar cycle, and the song's first and second
half agree on that place, every downbeat there opens a loop — as does any
downbeat after a fill 1.8× the song's median.

### Inputs

```
SongInput   { path: string, beats: f64[] (sec, rel. to song start), duration: f64 (sec),
              strength: f64[] }   // #[serde(default)]; one per beat, used by strongHits
ClipInput   { path: string, duration: f64 (sec, REQUIRED) }
GenerateSettings {   // camelCase over IPC
  fps: u32,
  ntsc: bool,        // true for 23.976 / 29.97 / 59.94
  width: u32,
  height: u32,
  seed: u64 | null,           // #[serde(default)]
  maxClipSecs: f64 | null,    // #[serde(default)]; null = no cap
  cutMode: "lastBeat" | "strongHits",   // #[serde(default)] = lastBeat (§5.2 step 3c)
}
```

### Output

```
Timeline {
  fps, ntsc, width, height,
  total_frames: i64,
  audio: AudioItem[],
  video: VideoSlot[],
  unused_clips: string[],     // filenames
}
AudioItem / VideoSlot { path, start, end, in_frame, out_frame }   // all frames
```

Clip duration is **essential** (we can't place a clip without its length). The
frontend must drop clips it couldn't measure and tell the user (they likely need
ffprobe on PATH).

---

## 7. XML output contract

Format: `xmeml` version 5, `<!DOCTYPE xmeml>`. Structure per clip item:
`name`, `duration`, `rate` (`timebase` + `ntsc`), `start`, `end`, `in`, `out`,
`file`. Sequence carries `rate` and video `samplecharacteristics`
(`width`/`height`/`rate`).

Rules the renderer already follows and must keep:
- **File dedup:** each unique media path gets one full `<file id="file-N">` on
  first appearance; later uses are `<file id="file-N"/>` references. IDs are
  assigned in document order (video first, then audio).
- **pathurl:** `file://localhost/<absolute-path>`, spaces `%20`-encoded, XML-
  escaped. ⚠️ Current encoding is minimal — Phase 4 must make it correct across
  OSes (Windows drive letters, full percent-encoding).
- **Frames are integers.** NTSC rates set `<ntsc>TRUE</ntsc>` with the rounded
  integer timebase (e.g. 30 for 29.97). Integer-fps path is validated; **NTSC
  needs a real Premiere round-trip test** (Phase 4).
- All names/paths pass through XML escaping.

---

## 8. Settled decisions

Clip-driven placement · contiguous video · seeded re-roll · each clip once ·
trim end to a beat at/before natural end — strong hits by default (kicks, the 1,
4-bar loop starts; 8-beat look-back), last beat as the least-trim option ·
overrun pushes next song, silent audio gaps allowed, last clip plays out ·
in-point always 0 · hard cuts · optional max-clip cap (off by default) · auto
fps/res from first clip with manual override · NTSC frames counted at the true
1000/1001 rate · unused clips reported by count + name.

---

## 9. Build plan (phased, with acceptance criteria)

### Phase 0 — Stand up the scaffold (do this first)
Make it actually build and run. Fix Tauri 2 boilerplate: capabilities/permission
strings, icons, lockfiles, plugin registration. Wire the dialog plugin on both
Rust and JS sides.
- **Accept:** `npm run tauri dev` launches the window; adding a song runs beat
  detection and shows a beat count; adding a clip shows its duration; Generate
  produces slots; Export writes an `.xml`.

### Phase 1 — Media probe (fps + resolution)
Extend `probe_duration` into a `probe_media` command returning
`{ duration, fps, width, height }` via ffprobe. Use the **first clip** to fill
the "Auto" frame-rate and sequence resolution (currently hard-coded 30 /
1920×1080 in `readSettings`). Manual override stays.
- **Accept:** with "Auto" selected, the exported sequence rate and dimensions
  match the first clip; missing ffprobe surfaces a clear, actionable message.

### Phase 2 — Golden test in Rust
Port `spec/golden_reference.py`'s fixture into a Rust test that builds the
timeline with shuffling disabled and asserts the XML equals
`spec/beatcut-sample.golden.xml`.
- **Accept:** `cargo test` passes; changing the algorithm breaks the test.

### Phase 3 — Preview + light edits
Visual timeline (already partly there) plus: **re-roll all**, **replace one
clip** (swap a single slot for another unused/used clip, keeping timing),
**lock** clips (a re-roll keeps locked slots in place), and **clip thumbnails**
(extract a frame via ffmpeg). No in-app A/V playback — the watch-through happens
in Premiere.
- **Accept:** user can re-roll, lock a slot and re-roll without it moving,
  replace a single clip, and see thumbnails; Export reflects edits.

### Phase 4 — Robustness
Cross-OS `file://` URLs; NTSC round-trip validation in Premiere; add media
characteristics to `<file>` to cut down relink prompts; bundle Python as a
frozen sidecar (PyInstaller) so users don't need a venv; graceful errors
throughout.
- **Accept:** exports import without manual relinking when media is in place; a
  packaged build runs on a machine with no Python installed.

### Phase 5 — Package & distribute
Icons, app metadata, signed builds for the target OS(es).

---

## 10. Risks & gotchas

- **madmom install is fussy** about numpy/scipy. Pin `numpy<2` in the venv. Point
  the app at the interpreter with `BEATCUT_PYTHON`. Long-term: bundle it (Phase 4).
- **NTSC / rational time.** xmeml stores integer frames + timebase + ntsc flag.
  Frames are counted at the true 1000/1001 rate (§5.2), so cuts don't drift
  against the audio, but 23.976/29.97 still need a real Premiere import test. If
  rounding error shows up, consider modelling time as exact fractions like the
  `@chatoctopus/timeline` library does.
- **file:// paths across OSes** — the current encoder is minimal (Phase 4).
- **Beat detection latency** — madmom is not instant on long tracks, and the
  strength analysis (bar tracking + percussive onsets) adds to it: about 15 s for
  a 2-minute song on Apple Silicon. Detect once per song and cache. For bars, use
  madmom's bar tracker on the detected beats, not `DBNDownBeatTrackingProcessor`:
  on hi-hat-heavy material that one tracked double tempo and split bars in half.
- **Loop starts in loop-based music can be subtle.** Lo-fi fills are small and
  often come only every 8 bars, so averaging fill strength per 4-bar position
  found loops in just 2 of 7 test songs. Looking at where the song's biggest
  fills cluster, and requiring both halves of the song to agree, finds them in 5
  of 7; on shuffled data a 60% bar still reported loops up to 5% of the time,
  while 70% keeps that near 1%. Songs with no consistent loop get no loop bonus
  (kicks and downbeats still carry strong hits). If every detected downbeat in a
  song has a weak kick, its bars may be a beat off; loop logic can't fix that.
- **Tauri 2 API churn** — verify plugin/permission specifics against live docs.

---

## 11. Testing

- **Golden fixture** (§5.4, Phase 2) is the primary guard on placement + XML.
- **Manual Premiere import** after each meaningful change: File ▸ Import the
  exported `.xml`, confirm contiguous video, on-beat cuts, correct audio gaps.
- **Unit-test the tricky bits** of `timeline.rs` in isolation: overrun push,
  last-song play-out, contiguous advance, unused reporting, sub-frame skip.

---

## 12. Still needs a human decision (minor)

- **Replace-one-clip source:** when the user replaces a single slot, should the
  replacement come only from currently-unused clips, or may it reuse a placed
  clip? (Affects the "each clip once" invariant during manual edits.)
- **Thumbnail count:** one frame per slot, or a few across the slot?

Neither blocks Phases 0–2.
