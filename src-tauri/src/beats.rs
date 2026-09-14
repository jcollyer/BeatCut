//! Runs the madmom beat-detection sidecar and parses its output.

use serde::{Deserialize, Serialize};
use std::process::Command;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BeatResult {
    pub beats: Vec<f64>,
    pub tempo: Option<f64>,
    pub duration: Option<f64>,
    /// Cut strength per beat for the "strong hits" cut mode (see
    /// detect_beats.py); empty when that analysis failed.
    #[serde(default)]
    pub strength: Vec<f64>,
}

/// Run `python detect_beats.py <audio_path>` and parse the JSON it prints.
///
/// `python` is the interpreter to use (e.g. the venv python), `script` is the
/// path to detect_beats.py on disk (we write the embedded copy to a temp file
/// at startup — see main.rs).
pub fn detect_beats(python: &str, script: &str, audio_path: &str) -> Result<BeatResult, String> {
    let output = Command::new(python)
        .arg(script)
        .arg(audio_path)
        .output()
        .map_err(|e| format!("could not launch python ('{python}'): {e}"))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        let stdout = String::from_utf8_lossy(&output.stdout);
        return Err(format!(
            "beat detection exited with an error.\nstderr: {stderr}\nstdout: {stdout}"
        ));
    }

    let stdout = String::from_utf8_lossy(&output.stdout);
    let value: serde_json::Value = serde_json::from_str(stdout.trim())
        .map_err(|e| format!("could not parse beat output as JSON: {e}\nraw: {stdout}"))?;

    if let Some(err) = value.get("error") {
        return Err(format!("beat detector reported: {err}"));
    }

    serde_json::from_value(value).map_err(|e| format!("unexpected beat output shape: {e}"))
}

/// What ffprobe reports about a clip. The video fields are None when the file
/// has no video stream.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MediaInfo {
    pub duration: Option<f64>,
    /// Measured frame rate, e.g. 29.97002997 for NTSC 29.97.
    pub fps: Option<f64>,
    /// Display size: rotation metadata is applied, so a clip stored rotated
    /// reports the size it plays at.
    pub width: Option<u32>,
    pub height: Option<u32>,
}

/// Probe a clip with ffprobe: container duration plus the first video stream's
/// frame rate and display size (cover art excluded). Errors are meant for the user.
pub fn probe_media(path: &str) -> Result<MediaInfo, String> {
    let output = Command::new("ffprobe")
        .args([
            "-v", "error",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            "-select_streams", "v",
            path,
        ])
        .output()
        .map_err(|e| match e.kind() {
            std::io::ErrorKind::NotFound => "ffprobe wasn't found. Install ffmpeg (on macOS: \
                 brew install ffmpeg), make sure ffprobe is on your PATH, then restart BeatCut."
                .to_string(),
            _ => format!("could not run ffprobe: {e}"),
        })?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        let reason = stderr.lines().rev().map(str::trim).find(|l| !l.is_empty());
        return Err(format!(
            "ffprobe couldn't read this file: {}",
            reason.unwrap_or("unknown error")
        ));
    }

    let value: serde_json::Value = serde_json::from_slice(&output.stdout)
        .map_err(|e| format!("could not parse ffprobe output: {e}"))?;

    let duration = value["format"]["duration"].as_str().and_then(|s| s.parse::<f64>().ok());
    // Cover art in an audio file is a video stream flagged attached_pic, not footage.
    let no_stream = serde_json::Value::Null;
    let stream = value["streams"]
        .as_array()
        .and_then(|streams| {
            streams.iter().find(|s| s["disposition"]["attached_pic"].as_i64() != Some(1))
        })
        .unwrap_or(&no_stream);
    let fps = parse_rate(&stream["avg_frame_rate"]).or_else(|| parse_rate(&stream["r_frame_rate"]));
    let mut width = stream["width"].as_u64().and_then(|w| u32::try_from(w).ok());
    let mut height = stream["height"].as_u64().and_then(|h| u32::try_from(h).ok());
    // Some cameras store frames on their side (e.g. 3384x6016 flagged -90 plays
    // as 6016x3384), so report the size the clip displays at.
    if rotation(stream).is_some_and(|deg| deg.rem_euclid(180.0) == 90.0) {
        std::mem::swap(&mut width, &mut height);
    }

    Ok(MediaInfo { duration, fps, width, height })
}

/// "30000/1001" → 29.97…; None for "0/0" or anything unparseable.
fn parse_rate(rate: &serde_json::Value) -> Option<f64> {
    let (num, den) = rate.as_str()?.split_once('/')?;
    let (num, den) = (num.parse::<f64>().ok()?, den.parse::<f64>().ok()?);
    (num > 0.0 && den > 0.0).then(|| num / den)
}

/// Rotation in degrees, from the display matrix (current ffprobe) or the legacy
/// `rotate` tag.
fn rotation(stream: &serde_json::Value) -> Option<f64> {
    stream["side_data_list"]
        .as_array()
        .and_then(|list| list.iter().find_map(|d| d["rotation"].as_f64()))
        .or_else(|| stream["tags"]["rotate"].as_str().and_then(|s| s.parse().ok()))
}
