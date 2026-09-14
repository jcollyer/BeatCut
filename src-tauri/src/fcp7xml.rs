//! Render a `Timeline` into FCP7 XML (xmeml v5) — the format Premiere Pro
//! imports via File > Import. NOT .fcpxml (that's modern Final Cut and Premiere
//! can't read it).
//!
//! This is deliberately minimal but valid: each unique source file is defined
//! once (name + pathurl + rate) and referenced by id afterwards. Premiere reads
//! the real media on import, so we don't need full media characteristics here —
//! though adding them reduces relink prompts (a good spec item).

use crate::timeline::Timeline;
use std::collections::HashMap;
use std::path::Path;

fn xml_escape(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&apos;")
}

fn file_name(path: &str) -> String {
    Path::new(path)
        .file_name()
        .map(|s| s.to_string_lossy().to_string())
        .unwrap_or_else(|| path.to_string())
}

/// Turn an absolute path into a file:// URL. Minimal encoding — enough for a
/// draft. NOTE for the spec: this needs proper per-OS handling (Windows drive
/// letters, full percent-encoding).
fn to_file_url(path: &str) -> String {
    let normalized = path.replace('\\', "/");
    let encoded = normalized.replace(' ', "%20");
    if encoded.starts_with('/') {
        format!("file://localhost{encoded}")
    } else {
        format!("file://localhost/{encoded}")
    }
}

fn rate_block(fps: u32, ntsc: bool, indent: &str) -> String {
    format!(
        "{indent}<rate>\n{indent}  <timebase>{fps}</timebase>\n{indent}  <ntsc>{}</ntsc>\n{indent}</rate>\n",
        if ntsc { "TRUE" } else { "FALSE" }
    )
}

pub fn render(timeline: &Timeline, sequence_name: &str) -> String {
    let fps = timeline.fps;
    let ntsc = timeline.ntsc;

    // Assign a stable file id to each unique path; first use gets a full def.
    let mut file_ids: HashMap<String, String> = HashMap::new();
    let mut next_file = 0usize;
    let mut seen: HashMap<String, bool> = HashMap::new();
    let mut file_id = |path: &str| -> (String, bool) {
        if let Some(id) = file_ids.get(path) {
            let first = !*seen.get(path).unwrap_or(&false);
            seen.insert(path.to_string(), true);
            (id.clone(), first)
        } else {
            let id = format!("file-{next_file}");
            next_file += 1;
            file_ids.insert(path.to_string(), id.clone());
            seen.insert(path.to_string(), true);
            (id, true) // first appearance
        }
    };

    let mut out = String::new();
    out.push_str("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n");
    out.push_str("<!DOCTYPE xmeml>\n");
    out.push_str("<xmeml version=\"5\">\n");
    out.push_str("  <sequence>\n");
    out.push_str(&format!("    <name>{}</name>\n", xml_escape(sequence_name)));
    out.push_str(&format!("    <duration>{}</duration>\n", timeline.total_frames));
    out.push_str(&rate_block(fps, ntsc, "    "));
    out.push_str("    <media>\n");

    // ---------------- video ----------------
    out.push_str("      <video>\n");
    out.push_str("        <format>\n");
    out.push_str("          <samplecharacteristics>\n");
    out.push_str(&rate_block(fps, ntsc, "            "));
    out.push_str(&format!("            <width>{}</width>\n", timeline.width));
    out.push_str(&format!("            <height>{}</height>\n", timeline.height));
    out.push_str("          </samplecharacteristics>\n");
    out.push_str("        </format>\n");
    out.push_str("        <track>\n");

    let mut clip_n = 0usize;
    for slot in &timeline.video {
        let (fid, first) = file_id(&slot.path);
        let name = file_name(&slot.path);
        out.push_str(&format!(
            "          <clipitem id=\"clipitem-{clip_n}\">\n"
        ));
        out.push_str(&format!("            <name>{}</name>\n", xml_escape(&name)));
        out.push_str(&format!(
            "            <duration>{}</duration>\n",
            slot.out_frame - slot.in_frame
        ));
        out.push_str(&rate_block(fps, ntsc, "            "));
        out.push_str(&format!("            <start>{}</start>\n", slot.start));
        out.push_str(&format!("            <end>{}</end>\n", slot.end));
        out.push_str(&format!("            <in>{}</in>\n", slot.in_frame));
        out.push_str(&format!("            <out>{}</out>\n", slot.out_frame));
        out.push_str(&file_element(&fid, first, &name, &slot.path, fps, ntsc, true));
        out.push_str("          </clipitem>\n");
        clip_n += 1;
    }
    out.push_str("        </track>\n");
    out.push_str("      </video>\n");

    // ---------------- audio ----------------
    out.push_str("      <audio>\n");
    out.push_str("        <track>\n");
    for (i, item) in timeline.audio.iter().enumerate() {
        let (fid, first) = file_id(&item.path);
        let name = file_name(&item.path);
        out.push_str(&format!("          <clipitem id=\"audioclip-{i}\">\n"));
        out.push_str(&format!("            <name>{}</name>\n", xml_escape(&name)));
        out.push_str(&format!(
            "            <duration>{}</duration>\n",
            item.out_frame - item.in_frame
        ));
        out.push_str(&rate_block(fps, ntsc, "            "));
        out.push_str(&format!("            <start>{}</start>\n", item.start));
        out.push_str(&format!("            <end>{}</end>\n", item.end));
        out.push_str(&format!("            <in>{}</in>\n", item.in_frame));
        out.push_str(&format!("            <out>{}</out>\n", item.out_frame));
        out.push_str(&file_element(&fid, first, &name, &item.path, fps, ntsc, false));
        out.push_str("          </clipitem>\n");
    }
    out.push_str("        </track>\n");
    out.push_str("      </audio>\n");

    out.push_str("    </media>\n");
    out.push_str("  </sequence>\n");
    out.push_str("</xmeml>\n");
    out
}

/// A <file> element — full definition on first appearance, id-only reference after.
fn file_element(
    fid: &str,
    first: bool,
    name: &str,
    path: &str,
    fps: u32,
    ntsc: bool,
    is_video: bool,
) -> String {
    if !first {
        return format!("            <file id=\"{fid}\"/>\n");
    }
    let media = if is_video {
        "              <media>\n                <video/>\n              </media>\n"
    } else {
        "              <media>\n                <audio/>\n              </media>\n"
    };
    format!(
        "            <file id=\"{fid}\">\n\
         {indent}<name>{name}</name>\n\
         {indent}<pathurl>{url}</pathurl>\n\
         {rate}\
         {media}\
         {end_indent}</file>\n",
        indent = "              ",
        end_indent = "            ",
        name = xml_escape(name),
        url = xml_escape(&to_file_url(path)),
        rate = rate_block(fps, ntsc, "              "),
        media = media,
    )
}
