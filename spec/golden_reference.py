# Faithful mirror of timeline.rs + fcp7xml.rs, on fixed sample data,
# to preview the exact XML structure. Clip order is a fixed "already shuffled"
# example so output is deterministic and matches the hand-walked case.
# build() takes ntsc and cut_mode like GenerateSettings; the fixture uses the defaults.
import os, xml.dom.minidom as minidom

EPS = 1e-9
HIT_WINDOW_BEATS = 8    # StrongHits look-back, in beats
HIT_TRIM_PENALTY = 0.1  # strength each extra beat of trim has to buy

def to_frame(sec, fps, ntsc=False): return round(sec * (fps * 1000 / 1001 if ntsc else fps))
def file_name(p): return p.replace("\\", "/").split("/")[-1]

def xml_escape(s):
    return (s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
             .replace('"',"&quot;").replace("'","&apos;"))

def to_file_url(path):
    n = path.replace("\\","/").replace(" ","%20")
    return ("file://localhost"+n) if n.startswith("/") else ("file://localhost/"+n)

def snap_end(song_start, beats, lower, upper):
    best = None
    for b in beats:
        absb = song_start + b
        if absb <= lower + EPS: continue
        if absb <= upper + EPS: best = absb
        else: break
    return best

def snap_strong(song_start, beats, strength, lower, upper):
    cands = [i for i, b in enumerate(beats) if lower + EPS < song_start + b <= upper + EPS][-HIT_WINDOW_BEATS:]
    best = None
    for k, i in enumerate(cands):
        score = strength[i] - HIT_TRIM_PENALTY * (len(cands) - 1 - k)
        if best is None or score >= best[1] - EPS: best = (i, score)
    return song_start + beats[best[0]] if best else None

def build(songs, clips, fps, max_clip=None, ntsc=False, cut_mode="lastBeat"):
    audio, video = [], []
    def push_audio(s, start):
        audio.append(dict(path=s["path"], start=to_frame(start,fps,ntsc),
                          end=to_frame(start+s["duration"],fps,ntsc),
                          in_f=0, out_f=to_frame(s["duration"],fps,ntsc)))
    si = 0
    song_start = 0.0
    song_end = songs[0]["duration"]
    push_audio(songs[0], 0.0)
    t = 0.0
    pos = 0
    while pos < len(clips):
        if t >= song_end - EPS:
            if si + 1 >= len(songs): break
            si += 1; song_start = t; song_end = song_start + songs[si]["duration"]
            push_audio(songs[si], song_start)
        clip = clips[pos]
        eff = min(clip["duration"], max_clip) if (max_clip and max_clip>0) else clip["duration"]
        natural_end = t + eff
        overruns = natural_end > song_end + EPS
        if overruns:
            clip_end = natural_end
        else:
            strength = songs[si].get("strength") or []
            if cut_mode == "strongHits" and len(strength) == len(songs[si]["beats"]):
                snapped = snap_strong(song_start, songs[si]["beats"], strength, t, natural_end)
            else:
                snapped = snap_end(song_start, songs[si]["beats"], t, natural_end)
            clip_end = snapped or natural_end
        sf, ef = to_frame(t,fps,ntsc), to_frame(clip_end,fps,ntsc)
        if ef <= sf: pos += 1; continue
        video.append(dict(path=clip["path"], start=sf, end=ef, in_f=0, out_f=ef-sf))
        pos += 1; t = clip_end
        if overruns:
            if si + 1 >= len(songs): break
            si += 1; song_start = clip_end; song_end = song_start + songs[si]["duration"]
            push_audio(songs[si], song_start)
    unused = [file_name(c["path"]) for c in clips[pos:]]
    total = max((video[-1]["end"] if video else 0), (audio[-1]["end"] if audio else 0))
    return dict(fps=fps, ntsc=ntsc, width=1920, height=1080,
                total_frames=total, audio=audio, video=video, unused=unused)

def rate(fps, ntsc, ind):
    return f"{ind}<rate>\n{ind}  <timebase>{fps}</timebase>\n{ind}  <ntsc>{'TRUE' if ntsc else 'FALSE'}</ntsc>\n{ind}</rate>\n"

def file_el(fid, first, name, path, fps, ntsc, is_video):
    if not first: return f'            <file id="{fid}"/>\n'
    media = ("              <media>\n                <video/>\n              </media>\n" if is_video
             else "              <media>\n                <audio/>\n              </media>\n")
    return (f'            <file id="{fid}">\n'
            f'              <name>{xml_escape(name)}</name>\n'
            f'              <pathurl>{xml_escape(to_file_url(path))}</pathurl>\n'
            f'{rate(fps,ntsc,"              ")}'
            f'{media}'
            f'            </file>\n')

def render(tl, seq_name):
    fps, ntsc = tl["fps"], tl["ntsc"]
    ids, seen, nxt = {}, {}, [0]
    def fid(path):
        if path in ids:
            first = not seen.get(path, False); seen[path]=True; return ids[path], first
        i = f"file-{nxt[0]}"; nxt[0]+=1; ids[path]=i; seen[path]=True; return i, True
    o = []
    o.append('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n<xmeml version="5">\n')
    o.append('  <sequence>\n')
    o.append(f'    <name>{xml_escape(seq_name)}</name>\n')
    o.append(f'    <duration>{tl["total_frames"]}</duration>\n')
    o.append(rate(fps,ntsc,"    "))
    o.append('    <media>\n      <video>\n        <format>\n          <samplecharacteristics>\n')
    o.append(rate(fps,ntsc,"            "))
    o.append(f'            <width>{tl["width"]}</width>\n            <height>{tl["height"]}</height>\n')
    o.append('          </samplecharacteristics>\n        </format>\n        <track>\n')
    for n, s in enumerate(tl["video"]):
        f, first = fid(s["path"]); name = file_name(s["path"])
        o.append(f'          <clipitem id="clipitem-{n}">\n')
        o.append(f'            <name>{xml_escape(name)}</name>\n')
        o.append(f'            <duration>{s["out_f"]-s["in_f"]}</duration>\n')
        o.append(rate(fps,ntsc,"            "))
        o.append(f'            <start>{s["start"]}</start>\n            <end>{s["end"]}</end>\n')
        o.append(f'            <in>{s["in_f"]}</in>\n            <out>{s["out_f"]}</out>\n')
        o.append(file_el(f, first, name, s["path"], fps, ntsc, True))
        o.append('          </clipitem>\n')
    o.append('        </track>\n      </video>\n      <audio>\n        <track>\n')
    for i, a in enumerate(tl["audio"]):
        f, first = fid(a["path"]); name = file_name(a["path"])
        o.append(f'          <clipitem id="audioclip-{i}">\n')
        o.append(f'            <name>{xml_escape(name)}</name>\n')
        o.append(f'            <duration>{a["out_f"]-a["in_f"]}</duration>\n')
        o.append(rate(fps,ntsc,"            "))
        o.append(f'            <start>{a["start"]}</start>\n            <end>{a["end"]}</end>\n')
        o.append(f'            <in>{a["in_f"]}</in>\n            <out>{a["out_f"]}</out>\n')
        o.append(file_el(f, first, name, a["path"], fps, ntsc, False))
        o.append('          </clipitem>\n')
    o.append('        </track>\n      </audio>\n    </media>\n  </sequence>\n</xmeml>\n')
    return "".join(o)

# ---- sample data ----
def beats(dur, step=0.5):
    out, x = [], step
    while x <= dur + 1e-9:
        out.append(round(x,3)); x += step
    return out

songs = [
    {"path":"/media/music/track_A.wav", "duration":12.0, "beats":beats(12.0)},
    {"path":"/media/music/track_B.wav", "duration":10.0, "beats":beats(10.0)},
]
clips = [
    {"path":"/media/drone/cliff_pan.mp4",     "duration":3.3},
    {"path":"/media/drone/coastline.mp4",     "duration":5.8},
    {"path":"/media/drone/forest rise.mp4",   "duration":2.2},   # space -> %20
    {"path":"/media/drone/river_bend.mp4",    "duration":4.5},   # overruns song A
    {"path":"/media/drone/city_dusk.mp4",     "duration":3.1},
    {"path":"/media/drone/mountain_ridge.mp4","duration":6.7},
    {"path":"/media/drone/desert_dunes.mp4",  "duration":5.0},   # overruns song B (last)
    {"path":"/media/drone/harbor_lights.mp4", "duration":4.0},   # unused
]

tl = build(songs, clips, fps=30)
xml = render(tl, "BeatCut rough cut")
_out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "beatcut-sample.golden.xml")
open(_out, "w").write(xml)

# validate well-formed
minidom.parseString(xml)

print("=== placement (frames @30fps) ===")
print("VIDEO (contiguous):")
for s in tl["video"]:
    print(f"  {s['start']:>4}->{s['end']:>4}  ({(s['end']-s['start'])/30:.2f}s)  {file_name(s['path'])}")
print("AUDIO (gaps allowed):")
for a in tl["audio"]:
    print(f"  {a['start']:>4}->{a['end']:>4}  {file_name(a['path'])}")
print("UNUSED:", tl["unused"])
print("TOTAL FRAMES:", tl["total_frames"], f"({tl['total_frames']/30:.2f}s)")
print("\nXML is well-formed. Wrote spec/beatcut-sample.golden.xml")
