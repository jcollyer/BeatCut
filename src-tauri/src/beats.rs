//! Runs the madmom beat-detection sidecar and parses its output.

use serde::{Deserialize, Serialize};
use std::process::Command;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BeatResult {
    pub beats: Vec<f64>,
    pub tempo: Option<f64>,
    pub duration: Option<f64>,
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

/// Best-effort media duration via ffprobe. Returns None if ffprobe is missing
/// or the file can't be read — callers should degrade gracefully.
pub fn probe_duration(path: &str) -> Option<f64> {
    let output = Command::new("ffprobe")
        .args([
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            path,
        ])
        .output()
        .ok()?;

    if !output.status.success() {
        return None;
    }

    let value: serde_json::Value = serde_json::from_slice(&output.stdout).ok()?;
    value
        .get("format")?
        .get("duration")?
        .as_str()?
        .parse::<f64>()
        .ok()
}
