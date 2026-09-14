import { invoke } from "@tauri-apps/api/core";
import { open, save } from "@tauri-apps/plugin-dialog";

// ------------------------------------------------------------------ state
const state = {
  audio: [], // { path, name, beats:[], duration, tempo }
  clips: [], // { path, name, duration|null, fps, width, height, probing, error }
  timeline: null, // last generated Timeline from the backend
};

// Sequence rates a probed clip rate snaps to. Non-integer rates are NTSC: they
// export as the rounded timebase with ntsc=TRUE.
const SEQUENCE_RATES = [23.976, 24, 25, 29.97, 30, 47.952, 48, 50, 59.94, 60, 100, 119.88, 120];
// What "Auto" falls back to when no clip has readable video info.
const FALLBACK = { rate: 30, width: 1920, height: 1080 };

const $ = (id) => document.getElementById(id);
const baseName = (p) => p.split(/[\\/]/).pop();
const esc = (s) =>
  String(s).replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]);
const fmtTime = (s) => {
  if (s == null) return "--:--";
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return `${m}:${sec.toString().padStart(2, "0")}`;
};
const removeButton = (list, index, name) =>
  `<button class="remove" type="button" data-list="${list}" data-index="${index}" aria-label="Remove ${esc(name)}" title="Remove">×</button>`;

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
    .map((a, i) => {
      const right = a.working
        ? `<span class="tc working">detecting…</span>`
        : a.error
        ? `<span class="tc" style="color:var(--danger)">error</span>`
        : `<span class="tc">${a.beats.length} beats · ${fmtTime(a.duration)}</span>`;
      return `<li><span class="name">${esc(a.name)}</span>${right}${removeButton("audio", i, a.name)}</li>`;
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
    const entry = { path, name: baseName(path), duration: null, probing: true };
    state.clips.push(entry);
    renderClipList();
    // ffprobe gives the duration (required to place a clip) plus the frame rate
    // and size that "Auto" reads from the first clip.
    invoke("probe_media", { path })
      .then((info) => {
        Object.assign(entry, info);
        if (!info.width) setStatus(`${entry.name} has no video. Songs go in Add music.`, true);
      })
      .catch((e) => {
        entry.error = String(e);
        setStatus(entry.error, true); // e.g. how to install ffprobe
      })
      .finally(() => {
        entry.probing = false;
        renderClipList();
        updateAutoLabels();
      });
  }
});

function renderClipList() {
  const ul = $("clipList");
  if (!state.clips.length) {
    ul.innerHTML = `<li class="empty">Add the video clips to draw from. Order doesn't matter.</li>`;
    return;
  }
  ul.innerHTML = state.clips
    .map((c, i) => {
      const right = c.probing
        ? `<span class="tc working">measuring…</span>`
        : c.error
        ? `<span class="tc" style="color:var(--danger)">error</span>`
        : !c.width
        ? `<span class="tc" style="color:var(--danger)">no video</span>`
        : `<span class="tc">${c.duration ? fmtTime(c.duration) : "—"}</span>`;
      const tip = c.probing
        ? ""
        : c.error || (c.width ? `${c.width}×${c.height} · ${c.fps ? +c.fps.toFixed(3) : "?"} fps` : "No video stream. Songs go in Add music.");
      return `<li title="${esc(tip)}"><span class="name">${esc(c.name)}</span>${right}${removeButton("clips", i, c.name)}</li>`;
    })
    .join("");
}

// Remove buttons in either bin. Changing the inputs invalidates the last cut.
for (const id of ["audioList", "clipList"]) {
  $(id).addEventListener("click", (e) => {
    const btn = e.target.closest("button.remove");
    if (!btn) return;
    const list = btn.dataset.list === "audio" ? state.audio : state.clips;
    const [removed] = list.splice(Number(btn.dataset.index), 1);
    if (!removed) return;
    const hadCut = state.timeline !== null;
    state.timeline = null;
    $("export").disabled = true;
    renderAudioList();
    renderClipList();
    renderTimeline();
    updateTempoReadout();
    updateAutoLabels();
    setStatus(`Removed ${removed.name}.${hadCut ? " Generate again to update the cut." : ""}`);
  });
}

// "Auto" frame rate and resolution come from the first clip with readable video.
function firstClipMedia(clips = state.clips) {
  return clips.find((c) => c.fps && c.width && c.height) || null;
}

function snapRate(fps) {
  const nearest = SEQUENCE_RATES.reduce((a, b) => (Math.abs(b - fps) < Math.abs(a - fps) ? b : a));
  return Math.abs(nearest - fps) < 0.05 ? nearest : Math.round(fps);
}

// 30 + ntsc → 29.97, for display.
const rateLabel = ({ fps, ntsc }) => (ntsc ? +((fps * 1000) / 1001).toFixed(3) : fps);

function updateAutoLabels() {
  const media = firstClipMedia();
  $("fps").querySelector('option[value="auto"]').textContent = media
    ? `Auto — ${snapRate(media.fps)} fps (first clip)`
    : "Auto (from first clip)";
  $("resolution").querySelector('option[value="auto"]').textContent = media
    ? `Auto — ${media.width} × ${media.height} (first clip)`
    : "Auto (from first clip)";
}

// ------------------------------------------------------------------ generate
function readSettings(media) {
  const fpsSel = $("fps").value;
  const rate = fpsSel === "auto" ? (media ? snapRate(media.fps) : FALLBACK.rate) : parseFloat(fpsSel);

  let width;
  let height;
  const resSel = $("resolution").value;
  if (resSel === "auto") {
    ({ width, height } = media || FALLBACK);
  } else if (resSel === "custom") {
    width = Math.max(16, parseInt($("resW").value, 10) || FALLBACK.width);
    height = Math.max(16, parseInt($("resH").value, 10) || FALLBACK.height);
  } else {
    [width, height] = resSel.split("x").map(Number);
  }

  const seedVal = $("seed").value.trim();
  const capOn = $("capOn").checked;
  return {
    fps: Math.round(rate),
    ntsc: !Number.isInteger(rate), // 23.976 / 29.97 / 59.94 → ntsc
    width,
    height,
    seed: seedVal ? Number(seedVal) : null,
    maxClipSecs: capOn ? Math.max(1, parseFloat($("maxClip").value) || 12) : null,
  };
}

// Custom resolution reveals width/height inputs, seeded from the first clip.
$("resolution").addEventListener("change", () => {
  const custom = $("resolution").value === "custom";
  $("customResField").hidden = !custom;
  const media = firstClipMedia();
  if (custom && media) {
    $("resW").value = media.width;
    $("resH").value = media.height;
  }
});

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
  if (state.clips.some((c) => c.probing)) {
    setStatus("Still measuring clips — hang on a sec.", true);
    return;
  }
  if (!state.audio.length || !state.clips.length) {
    setStatus("Add at least one song and one clip.", true);
    return;
  }

  // A clip needs a duration (ffprobe) and a video stream. Drop the rest and
  // say why: no duration usually means ffprobe is missing; no video usually
  // means a song went into the clips bin.
  const measured = state.clips.filter((c) => typeof c.duration === "number" && c.duration > 0);
  const usable = measured.filter((c) => c.width);
  const unmeasured = state.clips.length - measured.length;
  const noVideo = measured.length - usable.length;
  if (!usable.length) {
    setStatus(
      noVideo
        ? "None of the clips have video. Songs go in Add music, footage in Add clips."
        : "No clip durations — install ffmpeg/ffprobe so clips can be measured.",
      true
    );
    return;
  }

  const songs = state.audio.map((a) => ({
    path: a.path,
    beats: a.beats,
    duration: a.duration || (a.beats.length ? a.beats[a.beats.length - 1] : 0),
  }));
  const clips = usable.map((c) => ({ path: c.path, duration: c.duration }));
  const media = firstClipMedia(usable);
  const settings = readSettings(media);

  try {
    const timeline = await invoke("generate_timeline", { songs, clips, settings });
    state.timeline = timeline;
    renderTimeline();
    $("export").disabled = false;
    const unused = timeline.unused_clips || [];
    let msg = `${timeline.video.length} clips placed.`;
    if (unused.length) msg += ` ${unused.length} didn't fit: ${unused.join(", ")}.`;
    if (unmeasured) msg += ` (${unmeasured} clip(s) skipped — no duration.)`;
    if (noVideo) msg += ` (${noVideo} file(s) skipped — no video.)`;
    msg += ` Sequence: ${rateLabel(settings)} fps, ${settings.width}×${settings.height}.`;
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
