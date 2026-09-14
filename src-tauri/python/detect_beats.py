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
             + DOWNBEAT_BONUS * kick   on the 1 of a bar
             + PHRASE_BONUS   * kick   on the 1 that opens a 4-bar phrase

kick / snare: positive spectral flux of the percussive part (median-filter
HPSS) in a kick band and a snare band, strongest within ±40 ms of the beat,
normalized per song by the 90th percentile and capped at 1.
Bars: madmom's bar tracker run on the beats below, so a double-tempo guess
can't split bars in half.
Phrase starts: fills close a phrase, so each downbeat gets a fill score (drum
activity over the half bar before it, relative to the song's median). The
4-bar phase with clearly the strongest fills marks the phrase starts, and a
downbeat after an exceptionally big fill counts on its own.
"""
import sys
import json

SAMPLE_RATE = 44100
FRAME_SIZE = 2048
FPS = 100  # spectrogram frames per second
KICK_HZ = (40.0, 130.0)
SNARE_HZ = (1500.0, 5000.0)
ACTIVITY_HZ = (80.0, 5000.0)
MAX_HZ = 5000.0  # nothing above is used, so HPSS skips those bins
HPSS_SIZE = 17
BEAT_WINDOW_S = 0.04
SNARE_WEIGHT = 0.6
DOWNBEAT_BONUS = 0.25
PHRASE_BONUS = 0.5
PHRASE_BARS = 4
PHASE_CONFIDENCE = 1.2  # best phase's mean fill score vs the other phases
STRONG_FILL = 1.8  # a fill this far above the song's median counts on its own


def beat_strength(audio_path, beat_list):
    import numpy as np
    from madmom.audio.signal import FramedSignal, Signal
    from madmom.audio.spectrogram import Spectrogram
    from madmom.audio.stft import ShortTimeFourierTransform
    from madmom.features.downbeats import DBNBarTrackingProcessor, RNNBarProcessor
    from scipy.ndimage import median_filter

    beats = np.asarray(beat_list, dtype=float)
    if len(beats) < 2:
        return [0.0] * len(beats)

    # Positive spectral flux of the percussive part, frames x bins up to MAX_HZ.
    sig = Signal(audio_path, sample_rate=SAMPLE_RATE, num_channels=1)
    stft = ShortTimeFourierTransform(FramedSignal(sig, frame_size=FRAME_SIZE, fps=FPS))
    spec = np.asarray(Spectrogram(stft), dtype=np.float32)
    freqs = np.arange(spec.shape[1]) * SAMPLE_RATE / FRAME_SIZE
    keep = freqs <= MAX_HZ
    spec, freqs = spec[:, keep], freqs[keep]
    harmonic = median_filter(spec, size=(HPSS_SIZE, 1))  # smooth along time
    percussive = median_filter(spec, size=(1, HPSS_SIZE))  # smooth along frequency
    logp = np.log1p(spec * (percussive**2 / (percussive**2 + harmonic**2 + 1e-12)))
    # A silent frame in front so an onset right at the start still registers.
    flux = np.maximum(np.diff(logp, axis=0, prepend=np.zeros_like(logp[:1])), 0.0)

    def band(lo, hi):
        return flux[:, (freqs >= lo) & (freqs <= hi)].mean(axis=1)

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

    kick = normalize(at_beats(band(*KICK_HZ)))
    snare = normalize(at_beats(band(*SNARE_HZ)))
    activity = band(*ACTIVITY_HZ)

    # Position in the bar for each beat (1 = downbeat), from these beats.
    bar_pos = np.zeros(len(beats), dtype=int)
    for t, p in DBNBarTrackingProcessor(beats_per_bar=[3, 4])(RNNBarProcessor()((audio_path, beats))):
        i = int(np.argmin(np.abs(beats - t)))
        if abs(beats[i] - t) < 1e-3:
            bar_pos[i] = int(p)
    down_idx = np.flatnonzero(bar_pos == 1)
    downbeats = beats[down_idx]

    # Fill score per downbeat: drum activity over the half bar before it.
    fills = np.zeros(len(downbeats))
    for j in range(1, len(downbeats)):
        half_bar = (downbeats[j] - downbeats[j - 1]) / 2
        lo = int(round((downbeats[j] - half_bar) * FPS))
        hi = int(round((downbeats[j] - 0.02) * FPS))
        seg = activity[max(lo, 0): max(hi, 0)]
        fills[j] = seg.mean() if len(seg) else 0.0
    median_fill = np.median(fills[1:]) if len(fills) > 1 else 0.0
    if median_fill > 0:
        fills = fills / median_fill

    # Phrase starts: the 4-bar phase with clearly the strongest fills, plus any big fill.
    phrase_down = fills >= STRONG_FILL
    if len(fills) >= PHRASE_BARS * 2:
        means = [np.mean(fills[p::PHRASE_BARS][1:] if p == 0 else fills[p::PHRASE_BARS]) for p in range(PHRASE_BARS)]
        best = int(np.argmax(means))
        others = np.mean([m for p, m in enumerate(means) if p != best])
        if others > 0 and means[best] / others >= PHASE_CONFIDENCE:
            phrase_down[best::PHRASE_BARS] = True
    phrase = np.zeros(len(beats), dtype=bool)
    phrase[down_idx[phrase_down]] = True

    strength = np.maximum(kick, SNARE_WEIGHT * snare)
    strength = strength + DOWNBEAT_BONUS * kick * (bar_pos == 1)
    strength = strength + PHRASE_BONUS * kick * phrase
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
