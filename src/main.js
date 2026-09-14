import { invoke } from "@tauri-apps/api/core";
import { open, save } from "@tauri-apps/plugin-dialog";

// ------------------------------------------------------------------ state
const state = {
  audio: [], // { path, name, beats:[], duration, tempo }
  clips: [], // { path, name, duration|null }
  timeline: null, // last generated Timeline from the backend
};

const $ = (id) => document.getElementById(id);
const baseName = (p) => p.split(/[\\/]/).pop();
const fmtTime = (s) => {
  if (s == null) return "--:--";
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return `${m}:${sec.toString().padStart(2, "0")}`;
};

function setStatus(msg, isError = false) {
  const el = $("status");
  el.textContent = msg;
  el.classList.toggle("error", isError);
}

// ------------------------------------------------------------------ audio
$("addAudio").addEventListener("click", async () => {
  const picked = await open({
    multiple: true,
    filters: [{ name: "Audio", extensions: ["wav", "mp3", "aiff", "aif", "flac", "m4a", "mp4"] }],
  });
  if (!picked) return;
  const paths = Array.isArray(picked) ? picked : [picked];

  for (const path of paths) {
    const entry = { path, name: baseName(path), beats: [], duration: null, tempo: null, working: true };
    state.audio.push(entry);
    renderAudioList();
    try {
      const res = await invoke("detect_beats_cmd", { audioPath: path });
      entry.beats = res.beats || [];
      entry.tempo = res.tempo;
      entry.duration = res.duration;
    } catch (e) {
      entry.error = String(e);
      setStatus(`Beat detection failed for ${entry.name}`, true);
    } finally {
      entry.working = false;
      renderAudioList();
      renderTimeline();
      updateTempoReadout();
    }
  }
});

function updateTempoReadout() {
  const withTempo = state.audio.find((a) => a.tempo);
  const totalDur = state.audio.reduce((sum, a) => sum + (a.duration || 0), 0);
  $("tempoReadout").textContent = state.audio.length
    ? `${state.audio.length} track(s) · ${fmtTime(totalDur)}${withTempo ? ` · ~${withTempo.tempo} bpm` : ""}`
    : "no audio yet";
}

function renderAudioList() {
  const ul = $("audioList");
  if (!state.audio.length) {
    ul.innerHTML = `<li class="empty">Add the song stems or full tracks. Beats are detected on drop.</li>`;
    return;
  }
  ul.innerHTML = state.audio
    .map((a) => {
      const right = a.working
        ? `<span class="tc working">detecting…</span>`
        : a.error
        ? `<span class="tc" style="color:var(--danger)">error</span>`
        : `<span class="tc">${a.beats.length} beats · ${fmtTime(a.duration)}</span>`;
      return `<li><span class="name">${a.name}</span>${right}</li>`;
    })
    .join("");
}

// ------------------------------------------------------------------ clips
$("addClips").addEventListener("click", async () => {
  const picked = await open({
    multiple: true,
    filters: [{ name: "Video", extensions: ["mp4", "mov", "mkv", "m4v", "avi", "mxf"] }],
  });
  if (!picked) return;
  const paths = Array.isArray(picked) ? picked : [picked];

  for (const path of paths) {
    const entry = { path, name: baseName(path), duration: null };
    state.clips.push(entry);
    renderClipList();
    // duration is best-effort (needs ffprobe); fine if it stays null
    invoke("probe_duration", { path })
      .then((d) => {
        entry.duration = d;
        renderClipList();
      })
      .catch(() => {});
  }
});

function renderClipList() {
  const ul = $("clipList");
  if (!state.clips.length) {
    ul.innerHTML = `<li class="empty">Add the video clips to draw from. Order doesn't matter.</li>`;
    return;
  }
  ul.innerHTML = state.clips
    .map(
      (c) =>
        `<li><span class="name">${c.name}</span><span class="tc">${
          c.duration ? fmtTime(c.duration) : "—"
        }</span></li>`
    )
    .join("");
}

// ------------------------------------------------------------------ generate
function readSettings() {
  const fpsSel = $("fps").value;
  let fps = 30;
  let ntsc = false;
  if (fpsSel === "auto") {
    // TODO(ui phase): probe fps + resolution from the first clip via ffprobe.
    fps = 30;
    ntsc = false;
  } else {
    const raw = parseFloat(fpsSel);
    ntsc = !Number.isInteger(raw); // 23.976 / 29.97 → ntsc
    fps = Math.round(raw);
  }
  const seedVal = $("seed").value.trim();
  const capOn = $("capOn").checked;
  return {
    fps,
    ntsc,
    width: 1920, // TODO(ui phase): auto from first clip
    height: 1080,
    seed: seedVal ? Number(seedVal) : null,
    maxClipSecs: capOn ? Math.max(1, parseFloat($("maxClip").value) || 12) : null,
  };
}

// cap toggle shows/hides the seconds field
$("capOn").addEventListener("change", () => {
  $("capField").hidden = !$("capOn").checked;
});

// re-roll = new random seed, then generate
$("reroll").addEventListener("click", () => {
  $("seed").value = Math.floor(Math.random() * 1_000_000);
  $("generate").click();
});

$("generate").addEventListener("click", async () => {
  if (state.audio.some((a) => a.working)) {
    setStatus("Still detecting beats — hang on a sec.", true);
    return;
  }
  if (!state.audio.length || !state.clips.length) {
    setStatus("Add at least one song and one clip.", true);
    return;
  }

  // Clip duration is essential in this model. Drop clips we couldn't measure
  // and tell the user (they likely need ffprobe on PATH).
  const usable = state.clips.filter((c) => typeof c.duration === "number" && c.duration > 0);
  const unmeasured = state.clips.length - usable.length;
  if (!usable.length) {
    setStatus("No clip durations — install ffmpeg/ffprobe so clips can be measured.", true);
    return;
  }

  const songs = state.audio.map((a) => ({
    path: a.path,
    beats: a.beats,
    duration: a.duration || (a.beats.length ? a.beats[a.beats.length - 1] : 0),
  }));
  const clips = usable.map((c) => ({ path: c.path, duration: c.duration }));

  try {
    const timeline = await invoke("generate_timeline", { songs, clips, settings: readSettings() });
    state.timeline = timeline;
    renderTimeline();
    $("export").disabled = false;
    const unused = timeline.unused_clips || [];
    let msg = `${timeline.video.length} clips placed.`;
    if (unused.length) msg += ` ${unused.length} didn't fit: ${unused.join(", ")}.`;
    if (unmeasured) msg += ` (${unmeasured} clip(s) skipped — no duration.)`;
    setStatus(msg);
    firePulse();
  } catch (e) {
    setStatus(String(e), true);
  }
});

function firePulse() {
  const p = document.querySelector(".pulse");
  p.classList.remove("beating");
  void p.offsetWidth; // restart animation
  p.classList.add("beating");
}

// ------------------------------------------------------------------ export
$("export").addEventListener("click", async () => {
  if (!state.timeline) return;
  const outPath = await save({
    defaultPath: "beatcut-rough.xml",
    filters: [{ name: "Final Cut Pro 7 XML", extensions: ["xml"] }],
  });
  if (!outPath) return;
  try {
    await invoke("export_xml", {
      timeline: state.timeline,
      outPath,
      sequenceName: "BeatCut rough cut",
    });
    setStatus(`Exported → ${baseName(outPath)}. Import it in Premiere with File ▸ Import.`);
  } catch (e) {
    setStatus(String(e), true);
  }
});

// ------------------------------------------------------------------ render timeline
function renderTimeline() {
  const PX_PER_SEC = 26; // zoom; make this a control later
  const totalDur = state.audio.reduce((sum, a) => sum + (a.duration || 0), 0);

  // audio lane: one block per file, beat ticks overlaid
  const audioLane = $("audioLane");
  audioLane.innerHTML = "";
  let cursor = 0;
  for (const a of state.audio) {
    if (!a.duration) continue;
    const block = document.createElement("div");
    block.className = "audio-block";
    block.style.left = `${cursor * PX_PER_SEC}px`;
    block.style.width = `${a.duration * PX_PER_SEC}px`;
    block.innerHTML = `<span class="cap">${a.name}</span>`;
    for (const b of a.beats) {
      const tick = document.createElement("div");
      tick.className = "beat-tick";
      tick.style.left = `${b * PX_PER_SEC}px`;
      block.appendChild(tick);
    }
    audioLane.appendChild(block);
    cursor += a.duration;
  }
  audioLane.style.minWidth = `${Math.max(totalDur * PX_PER_SEC, 200)}px`;

  // video lane: generated slots
  const videoLane = $("videoLane");
  if (!state.timeline || !state.timeline.video.length) {
    videoLane.innerHTML = `<p class="lane-empty">Your beat-synced slots land here once you generate.</p>`;
    videoLane.style.minWidth = audioLane.style.minWidth;
    return;
  }
  const fps = state.timeline.fps;
  videoLane.innerHTML = "";
  for (const slot of state.timeline.video) {
    const el = document.createElement("div");
    el.className = "slot";
    const startSec = slot.start / fps;
    const lenSec = (slot.end - slot.start) / fps;
    el.style.left = `${startSec * PX_PER_SEC}px`;
    el.style.width = `${Math.max(lenSec * PX_PER_SEC - 2, 4)}px`;
    el.innerHTML = `<span class="slot-name">${baseName(slot.path)}</span>`;
    el.title = `${baseName(slot.path)} · in ${slot.in_frame}f · ${slot.end - slot.start}f`;
    videoLane.appendChild(el);
  }
  videoLane.style.minWidth = audioLane.style.minWidth;
}

// initial paint
renderAudioList();
renderClipList();
renderTimeline();
