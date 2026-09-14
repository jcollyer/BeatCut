// Prevents an extra console window on Windows in release.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod beats;
mod fcp7xml;
mod timeline;

use beats::BeatResult;
use std::fs;
use std::sync::OnceLock;
use timeline::{ClipInput, GenerateSettings, SongInput, Timeline};

// The Python detector is embedded in the binary and written to a temp file on
// first use, so we never depend on the working directory.
const DETECT_PY: &str = include_str!("../python/detect_beats.py");
static SCRIPT_PATH: OnceLock<String> = OnceLock::new();

fn script_path() -> String {
    SCRIPT_PATH
        .get_or_init(|| {
            let dir = std::env::temp_dir().join("beatcut");
            let _ = fs::create_dir_all(&dir);
            let path = dir.join("detect_beats.py");
            let _ = fs::write(&path, DETECT_PY);
            path.to_string_lossy().to_string()
        })
        .clone()
}

/// Which python to call. Override with BEATCUT_PYTHON (e.g. the venv python).
fn python_bin() -> String {
    std::env::var("BEATCUT_PYTHON").unwrap_or_else(|_| "python3".to_string())
}

// Both of these block on a child process (madmom can take many seconds), so run
// them on Tauri's thread pool: plain sync commands execute on the main thread
// and would freeze the window until they return.
#[tauri::command(async)]
fn detect_beats_cmd(audio_path: String) -> Result<BeatResult, String> {
    beats::detect_beats(&python_bin(), &script_path(), &audio_path)
}

#[tauri::command(async)]
fn probe_duration(path: String) -> Option<f64> {
    beats::probe_duration(&path)
}

#[tauri::command]
fn generate_timeline(
    songs: Vec<SongInput>,
    clips: Vec<ClipInput>,
    settings: GenerateSettings,
) -> Result<Timeline, String> {
    timeline::build_timeline(&songs, &clips, &settings)
}

#[tauri::command]
fn export_xml(timeline: Timeline, out_path: String, sequence_name: String) -> Result<(), String> {
    let xml = fcp7xml::render(&timeline, &sequence_name);
    fs::write(&out_path, xml).map_err(|e| format!("could not write {out_path}: {e}"))
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .invoke_handler(tauri::generate_handler![
            detect_beats_cmd,
            probe_duration,
            generate_timeline,
            export_xml
        ])
        .run(tauri::generate_context!())
        .expect("error while running BeatCut");
}
