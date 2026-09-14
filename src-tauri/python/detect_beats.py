#!/usr/bin/env python3
"""Beat detection sidecar for BeatCut.

Takes one audio file path, prints a single JSON object to stdout:
    {"beats": [<seconds>, ...], "tempo": <bpm or null>, "duration": <seconds or null>,
     "strength": [<cut strength per beat>, ...]}

On failure prints {"error": "..."} and exits 1. If only the strength analysis
fails, "strength" is [] (the app then cuts on the last beat for that song) and
the reason goes to stderr.

madmom notes: madmom can be fussy about numpy/scipy versions. If you hit
install errors, see README (numpy<2, madmom pinned from GitHub).

Strength feeds the "strong hits" cut mode; it scores how good a cut each beat is:

    strength = max(kick, SNARE_WEIGHT * snare)
             + DOWNBEAT_BONUS * kick                     on the 1 of a bar
             + LOOP_BONUS * min(1, kick / CLEAR_KICK)    on the 1 that opens a 4-bar loop

kick / snare: positive spectral flux of the percussive part (median-filter
HPSS) in a kick band and a snare band, strongest within ±40 ms of the beat,
normalized per song by the 90th percentile and capped at 1. Any clear kick
earns the full loop bonus: the fill just before a loop start leaves low-end
energy that makes that kick measure weaker than it sounds.
Bars: madmom's bar tracker run on the beats below, so a double-tempo guess
can't split bars in half.
Loop starts: a fill or build-up closes a loop, so each downbeat gets a fill
score (full-mix spectral flux over the half bar before it, relative to the
song's median). When most of the song's biggest fills sit at the same place in
the 4-bar cycle, and the first and second half of the song agree on it, every
downbeat at that place opens a loop. A downbeat after an exceptionally big fill
counts on its own either way.
"""
import sys
import json

SAMPLE_RATE = 44100
FRAME_SIZE = 2048
FPS = 100  # spectrogram frames per second
KICK_HZ = (40.0, 130.0)
SNARE_HZ = (1500.0, 5000.0)
FILL_HZ = (80.0, 5000.0)
MAX_HZ = 5000.0  # nothing above is used, so HPSS skips those bins
HPSS_SIZE = 17
BEAT_WINDOW_S = 0.04
SNARE_WEIGHT = 0.6
DOWNBEAT_BONUS = 0.25
LOOP_BONUS = 0.5
CLEAR_KICK = 0.6  # a loop start's kick at or above this earns the whole loop bonus
LOOP_BARS = 4
TOP_FILLS = 0.15  # a song's biggest fills: this share of its downbeats (at least 4)
MIN_LOOP_SHARE = 0.7  # how many of those must sit at one place in the loop (chance: 25%)
STRONG_FILL = 1.8  # a fill this far above the song's median counts on its own


def fill_scores(downbeats, activity):
    """Mean activity over the half bar before each downbeat, relative to the median.

    Downbeat 0 has no bar before it and scores 0.
    """
    import numpy as np

    fills = np.zeros(len(downbeats))
    for j in range(1, len(downbeats)):
        half_bar = (downbeats[j] - downbeats[j - 1]) / 2
        lo = int(round((downbeats[j] - half_bar) * FPS))
        hi = int(round((downbeats[j] - 0.02) * FPS))
        seg = activity[max(lo, 0): max(hi, 0)]
        fills[j] = seg.mean() if len(seg) else 0.0
    median_fill = np.median(fills[1:]) if len(fills) > 1 else 0.0
    return fills / median_fill if median_fill > 0 else fills


def loop_position(fills):
    """(position, share) from a song's fill scores, one per downbeat.

    share is the fraction of the song's biggest fills that sit at the most common
    place in the 4-bar cycle (downbeat index mod LOOP_BARS). position is that
    place, or None unless share reaches MIN_LOOP_SHARE and the first and second
    half of the song pick the same place.
    """
    import math
    import numpy as np

    def most_common_place(idx):
        if len(idx) < 8:
            return None, 0.0
        top = max(4, math.ceil(TOP_FILLS * len(idx)))
        biggest = idx[np.argsort(fills[idx], kind="stable")[-top:]]
        counts = np.bincount(biggest % LOOP_BARS, minlength=LOOP_BARS)
        return int(np.argmax(counts)), float(counts.max() / top)

    idx = np.arange(1, len(fills))  # downbeat 0 has no fill score
    position, share = most_common_place(idx)
    first, _ = most_common_place(idx[idx < len(fills) // 2])
    second, _ = most_common_place(idx[idx >= len(fills) // 2])
    if position is None or share < MIN_LOOP_SHARE or not first == position == second:
        return None, share
    return position, share


def analyse_beats(audio_path, beat_list):
    """Per-beat kick, snare, bar position and loop starts for `beat_list` (seconds).

    Also returns the fill score per downbeat and the loop position found (None when
    the song shows no consistent 4-bar loop).
    """
    import numpy as np
    from madmom.audio.signal import FramedSignal, Signal
    from madmom.audio.spectrogram import Spectrogram
    from madmom.audio.stft import ShortTimeFourierTransform
    from madmom.features.downbeats import DBNBarTrackingProcessor, RNNBarProcessor
    from scipy.ndimage import median_filter

    beats = np.asarray(beat_list, dtype=float)

    # Magnitude spectrogram up to MAX_HZ; spectral flux of its percussive part
    # scores kicks and snares, flux of the full mix scores fills.
    sig = Signal(audio_path, sample_rate=SAMPLE_RATE, num_channels=1)
    stft = ShortTimeFourierTransform(FramedSignal(sig, frame_size=FRAME_SIZE, fps=FPS))
    spec = np.asarray(Spectrogram(stft), dtype=np.float32)
    freqs = np.arange(spec.shape[1]) * SAMPLE_RATE / FRAME_SIZE
    keep = freqs <= MAX_HZ
    spec, freqs = spec[:, keep], freqs[keep]
    harmonic = median_filter(spec, size=(HPSS_SIZE, 1))  # smooth along time
    percussive = median_filter(spec, size=(1, HPSS_SIZE))  # smooth along frequency
    percussive_log = np.log1p(spec * (percussive**2 / (percussive**2 + harmonic**2 + 1e-12)))

    def flux(log_spec):
        # A silent frame in front so an onset right at the start still registers.
        return np.maximum(np.diff(log_spec, axis=0, prepend=np.zeros_like(log_spec[:1])), 0.0)

    def band(frames, lo, hi):
        return frames[:, (freqs >= lo) & (freqs <= hi)].mean(axis=1)

    def at_beats(env):
        w = int(round(BEAT_WINDOW_S * FPS))
        out = []
        for b in beats:
            i = int(round(b * FPS))
            seg = env[max(0, i - w): i + w + 1]
            out.append(float(seg.max()) if len(seg) else 0.0)
        return np.array(out)

    def normalize(v):
        ref = np.percentile(v, 90)
        return np.clip(v / ref, 0.0, 1.0) if ref > 0 else np.zeros_like(v)

    hits = flux(percussive_log)
    kick = normalize(at_beats(band(hits, *KICK_HZ)))
    snare = normalize(at_beats(band(hits, *SNARE_HZ)))

    # Position in the bar for each beat (1 = downbeat), from these beats.
    bar_pos = np.zeros(len(beats), dtype=int)
    for t, p in DBNBarTrackingProcessor(beats_per_bar=[3, 4])(RNNBarProcessor()((audio_path, beats))):
        i = int(np.argmin(np.abs(beats - t)))
        if abs(beats[i] - t) < 1e-3:
            bar_pos[i] = int(p)
    down_idx = np.flatnonzero(bar_pos == 1)

    # Loop starts: every downbeat at the song's loop position, plus any big fill.
    fills = fill_scores(beats[down_idx], band(flux(np.log1p(spec)), *FILL_HZ))
    position, share = loop_position(fills)
    loop_down = fills >= STRONG_FILL
    if position is not None:
        loop_down[position::LOOP_BARS] = True
    loop_start = np.zeros(len(beats), dtype=bool)
    loop_start[down_idx[loop_down]] = True

    return {"kick": kick, "snare": snare, "bar_pos": bar_pos, "fills": fills,
            "loop_position": position, "loop_share": share, "loop_start": loop_start}


def beat_strength(audio_path, beat_list):
    """Cut strength per beat, as described at the top of this file."""
    import numpy as np

    if len(beat_list) < 2:
        return [0.0] * len(beat_list)
    a = analyse_beats(audio_path, beat_list)
    kick = a["kick"]
    strength = (
        np.maximum(kick, SNARE_WEIGHT * a["snare"])
        + DOWNBEAT_BONUS * kick * (a["bar_pos"] == 1)
        + LOOP_BONUS * np.minimum(1.0, kick / CLEAR_KICK) * a["loop_start"]
    )
    return [round(float(s), 4) for s in strength]


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "usage: detect_beats.py <audio_file>"}))
        sys.exit(1)

    audio_path = sys.argv[1]

    try:
        from madmom.features.beats import RNNBeatProcessor, DBNBeatTrackingProcessor

        # 1) neural activation function, 2) DBN tracks beats from it
        activations = RNNBeatProcessor()(audio_path)
        tracker = DBNBeatTrackingProcessor(fps=100)
        beats = tracker(activations)  # numpy array of times (seconds)

        beat_list = [round(float(b), 6) for b in beats]

        # crude tempo estimate from the median inter-beat interval
        tempo = None
        if len(beat_list) > 1:
            import statistics
            diffs = [beat_list[i + 1] - beat_list[i] for i in range(len(beat_list) - 1)]
            med = statistics.median(diffs)
            if med > 0:
                tempo = round(60.0 / med, 2)

        # duration straight from the decoded signal, fall back to last beat
        duration = None
        try:
            from madmom.audio.signal import Signal
            sig = Signal(audio_path)
            duration = round(len(sig) / float(sig.sample_rate), 6)
        except Exception:
            duration = beat_list[-1] if beat_list else None

        # per-beat cut strength; optional, so a failure here keeps the beats
        strength = []
        try:
            strength = beat_strength(audio_path, beat_list)
        except Exception as exc:  # noqa: BLE001 - the app falls back to the last beat
            print(f"strength analysis failed: {exc}", file=sys.stderr)

        print(json.dumps({"beats": beat_list, "tempo": tempo, "duration": duration, "strength": strength}))
    except Exception as exc:  # noqa: BLE001 - surface any error to the Rust side
        print(json.dumps({"error": str(exc)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
