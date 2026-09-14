//! Clip-driven timeline builder — BeatCut's core.
//!
//! Model (agreed spec):
//! * Video track is the master and is ALWAYS contiguous — never a gap.
//! * Songs play in order; song 0 starts at t=0.
//! * Clips are shuffled (seeded) and used ONCE, laid left-to-right from t=0.
//! * A clip keeps its full length but its END is trimmed back to a beat AT OR
//!   BEFORE its natural end, picked by the cut mode:
//!   - LastBeat: the nearest such beat (trims the least; the next clip then
//!     starts on that beat).
//!   - StrongHits: among the last HIT_WINDOW_BEATS such beats, the one with the
//!     best per-beat strength from the song analysis (kicks, the 1 of a bar,
//!     phrase starts — see detect_beats.py), minus HIT_TRIM_PENALTY for each
//!     beat of extra trim. Songs without strength fall back to LastBeat.
//! * If a clip's natural end runs past the current song's end, it OVERRUNS:
//!   it plays full length (unsnapped — there are no beats in the silence), and
//!   the next song's start is PUSHED to line up with the clip's end. This opens
//!   a silent gap on the AUDIO track, which is allowed.
//! * On the last song, an overrunning final clip plays out over silence, then
//!   we stop.
//! * Stop when clips run out or audio (songs) run out. Leftover clips are
//!   reported as unused.
//! * Frames are counted at the real rate: an NTSC timebase runs 1000/1001 slow.

use rand::rngs::StdRng;
use rand::seq::SliceRandom;
use rand::SeedableRng;
use serde::{Deserialize, Serialize};
use std::path::Path;

const EPS: f64 = 1e-9;
/// StrongHits looks back at most this many beats (two bars of 4/4)...
const HIT_WINDOW_BEATS: usize = 8;
/// ...and a beat further back must beat a later one by this much strength per beat.
const HIT_TRIM_PENALTY: f64 = 0.1;

/// One song. `beats` are in seconds relative to the song's own start.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SongInput {
    pub path: String,
    pub beats: Vec<f64>,
    pub duration: f64,
    /// Cut strength per beat, same order as `beats`, for CutMode::StrongHits.
    /// Empty (or not one value per beat) means this song cuts on the last beat.
    #[serde(default)]
    pub strength: Vec<f64>,
}

/// One source clip. Duration is REQUIRED in this model — we can't place a clip
/// without knowing how long it is (so ffprobe, or another duration source, is
/// now mandatory rather than optional).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ClipInput {
    pub path: String,
    pub duration: f64,
}

/// Where a clip that ends inside a song gets its cut.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub enum CutMode {
    /// The last beat at or before the natural end.
    #[default]
    LastBeat,
    /// The strongest recent beat, weighed against the extra trim.
    StrongHits,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct GenerateSettings {
    pub fps: u32,
    /// true for 23.976 / 29.97 / 59.94.
    pub ntsc: bool,
    pub width: u32,
    pub height: u32,
    /// Re-roll = new seed. None = fresh entropy each time.
    #[serde(default)]
    pub seed: Option<u64>,
    /// Optional cap so a long clip can't drag. None = keep full length.
    #[serde(default)]
    pub max_clip_secs: Option<f64>,
    /// Where cuts inside a song land. Defaults to the last beat.
    #[serde(default)]
    pub cut_mode: CutMode,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AudioItem {
    pub path: String,
    pub start: i64, // timeline position, frames (may be > previous end: a gap)
    pub end: i64,
    pub in_frame: i64,
    pub out_frame: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct VideoSlot {
    pub path: String,
    pub start: i64, // frames; always == previous slot's end (contiguous)
    pub end: i64,
    pub in_frame: i64,  // always 0 — clips play from their start
    pub out_frame: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Timeline {
    pub fps: u32,
    pub ntsc: bool,
    pub width: u32,
    pub height: u32,
    pub total_frames: i64,
    pub audio: Vec<AudioItem>,
    pub video: Vec<VideoSlot>,
    /// Filenames of clips that didn't fit (audio ran out first).
    pub unused_clips: Vec<String>,
}

/// Seconds to frames at the real rate. An NTSC "30" timebase is 30000/1001 fps;
/// counting it at 30 would put cuts about a frame late every 33 seconds.
fn to_frame(seconds: f64, fps: u32, ntsc: bool) -> i64 {
    let rate = if ntsc { fps as f64 * 1000.0 / 1001.0 } else { fps as f64 };
    (seconds * rate).round() as i64
}

fn file_name(path: &str) -> String {
    Path::new(path)
        .file_name()
        .map(|s| s.to_string_lossy().to_string())
        .unwrap_or_else(|| path.to_string())
}

/// Largest absolute beat strictly greater than `lower` and <= `upper`.
/// `beats` must be ascending and relative to `song_start`.
fn snap_end(song_start: f64, beats: &[f64], lower: f64, upper: f64) -> Option<f64> {
    let mut best = None;
    for b in beats {
        let abs = song_start + b;
        if abs <= lower + EPS {
            continue;
        }
        if abs <= upper + EPS {
            best = Some(abs); // ascending, so this keeps climbing to the largest
        } else {
            break;
        }
    }
    best
}

/// StrongHits: of the last HIT_WINDOW_BEATS beats strictly after `lower` and
/// <= `upper`, the one with the best `strength` minus HIT_TRIM_PENALTY per beat
/// back from the latest. Scores within EPS tie, and ties go to the later beat.
/// `beats` must be ascending and relative to `song_start`, `strength` aligned.
fn snap_strong(
    song_start: f64,
    beats: &[f64],
    strength: &[f64],
    lower: f64,
    upper: f64,
) -> Option<f64> {
    let candidates: Vec<usize> = (0..beats.len())
        .filter(|&i| {
            let abs = song_start + beats[i];
            abs > lower + EPS && abs <= upper + EPS
        })
        .collect();
    let window = &candidates[candidates.len().saturating_sub(HIT_WINDOW_BEATS)..];
    let mut best: Option<(usize, f64)> = None;
    for (k, &i) in window.iter().enumerate() {
        let beats_back = (window.len() - 1 - k) as f64;
        let score = strength[i] - HIT_TRIM_PENALTY * beats_back;
        if best.map_or(true, |(_, top)| score >= top - EPS) {
            best = Some((i, score));
        }
    }
    best.map(|(i, _)| song_start + beats[i])
}

pub fn build_timeline(
    songs: &[SongInput],
    clips: &[ClipInput],
    settings: &GenerateSettings,
) -> Result<Timeline, String> {
    if songs.is_empty() {
        return Err("add at least one song first".into());
    }
    if clips.is_empty() {
        return Err("add at least one clip first".into());
    }

    let fps = settings.fps;
    let ntsc = settings.ntsc;

    // Seeded shuffle so a re-roll is just a new seed.
    let mut rng: StdRng = match settings.seed {
        Some(s) => StdRng::seed_from_u64(s),
        None => StdRng::from_entropy(),
    };
    let mut order: Vec<usize> = (0..clips.len()).collect();
    order.shuffle(&mut rng);

    // Defensive: sort each song's beats once, carrying strength along. Strength
    // that isn't one value per beat is dropped (that song cuts on the last beat).
    let sorted: Vec<(Vec<f64>, Option<Vec<f64>>)> = songs
        .iter()
        .map(|s| {
            let aligned = s.strength.len() == s.beats.len();
            let mut pairs: Vec<(f64, f64)> = s
                .beats
                .iter()
                .enumerate()
                .map(|(i, &b)| (b, if aligned { s.strength[i] } else { 0.0 }))
                .collect();
            pairs.sort_by(|x, y| x.0.partial_cmp(&y.0).unwrap());
            let beats = pairs.iter().map(|p| p.0).collect();
            let strength = aligned.then(|| pairs.iter().map(|p| p.1).collect());
            (beats, strength)
        })
        .collect();

    let mut audio_items: Vec<AudioItem> = Vec::new();
    let mut video: Vec<VideoSlot> = Vec::new();

    // Enter song 0 at t=0.
    let mut song_idx = 0usize;
    let mut song_start = 0.0_f64;
    let mut song_end = song_start + songs[0].duration;
    let push_audio = |items: &mut Vec<AudioItem>, s: &SongInput, start: f64| {
        items.push(AudioItem {
            path: s.path.clone(),
            start: to_frame(start, fps, ntsc),
            end: to_frame(start + s.duration, fps, ntsc),
            in_frame: 0,
            out_frame: to_frame(s.duration, fps, ntsc),
        });
    };
    push_audio(&mut audio_items, &songs[0], song_start);

    let mut t = 0.0_f64; // where the next clip starts (= end of previous clip)
    let mut clip_pos = 0usize;

    while clip_pos < order.len() {
        // Contiguous advance: a clip landed exactly on the song end.
        if t >= song_end - EPS {
            if song_idx + 1 >= songs.len() {
                break; // audio is over
            }
            song_idx += 1;
            song_start = t; // contiguous (t == previous song_end)
            song_end = song_start + songs[song_idx].duration;
            push_audio(&mut audio_items, &songs[song_idx], song_start);
        }

        let clip = &clips[order[clip_pos]];
        let eff_len = match settings.max_clip_secs {
            Some(m) if m > 0.0 => clip.duration.min(m),
            _ => clip.duration,
        };
        let natural_end = t + eff_len;

        let overruns = natural_end > song_end + EPS;
        let (beats, strength) = &sorted[song_idx];
        let clip_end = if overruns {
            natural_end // play full over the coming silence
        } else {
            let snapped = match (settings.cut_mode, strength) {
                (CutMode::StrongHits, Some(strength)) => {
                    snap_strong(song_start, beats, strength, t, natural_end)
                }
                _ => snap_end(song_start, beats, t, natural_end),
            };
            snapped.unwrap_or(natural_end)
        };

        let sf = to_frame(t, fps, ntsc);
        let ef = to_frame(clip_end, fps, ntsc);
        if ef <= sf {
            // sub-frame clip — can't render; treat as unused and move on.
            clip_pos += 1;
            continue;
        }
        video.push(VideoSlot {
            path: clip.path.clone(),
            start: sf,
            end: ef,
            in_frame: 0,
            out_frame: ef - sf,
        });
        clip_pos += 1;
        t = clip_end;

        // Handle an overrun by pushing the next song (or stopping if last).
        if overruns {
            if song_idx + 1 >= songs.len() {
                break; // last song: final clip played out, we're done
            }
            song_idx += 1;
            song_start = clip_end; // push — this is the silent gap
            song_end = song_start + songs[song_idx].duration;
            push_audio(&mut audio_items, &songs[song_idx], song_start);
        }
    }

    let unused_clips: Vec<String> = order[clip_pos..]
        .iter()
        .map(|&i| file_name(&clips[i].path))
        .collect();

    let last_video_end = video.last().map(|v| v.end).unwrap_or(0);
    let last_audio_end = audio_items.last().map(|a| a.end).unwrap_or(0);
    let total_frames = last_video_end.max(last_audio_end);

    Ok(Timeline {
        fps,
        ntsc,
        width: settings.width,
        height: settings.height,
        total_frames,
        audio: audio_items,
        video,
        unused_clips,
    })
}
