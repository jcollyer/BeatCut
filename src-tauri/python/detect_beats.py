#!/usr/bin/env python3
"""Beat detection sidecar for BeatCut.

Takes one audio file path, prints a single JSON object to stdout:
    {"beats": [<seconds>, ...], "tempo": <bpm or null>, "duration": <seconds or null>}

On failure prints {"error": "..."} and exits 1.

madmom notes: madmom can be fussy about numpy/scipy versions. If you hit
install errors, pin numpy<2 in the venv (see README). DBNBeatTrackingProcessor
gives good musical beats; swap in DBNDownBeatTrackingProcessor later if you want
downbeats (bar starts) so cuts can favour the "1".
"""
import sys
import json


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

        print(json.dumps({"beats": beat_list, "tempo": tempo, "duration": duration}))
    except Exception as exc:  # noqa: BLE001 - surface any error to the Rust side
        print(json.dumps({"error": str(exc)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
