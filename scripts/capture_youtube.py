#!/usr/bin/env python3
"""Capture a YouTube video tutorial as local markdown + images + publication-grade PDF."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html import unescape
from pathlib import Path

import requests
import yt_dlp

try:
    from youtube_transcript_api import YouTubeTranscriptApi
except ImportError:
    YouTubeTranscriptApi = None

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# Pygments 5-color white-panel code styling
CODE_COLOR_KEYWORD = "#0000FF"  # blue
CODE_COLOR_STRING = "#C41A16"   # red
CODE_COLOR_COMMENT = "#008000"  # green
CODE_COLOR_NAME = "#AF00DB"     # magenta (classes, builtins, numbers)
CODE_COLOR_DEFAULT = "#000000"  # black
CODE_PANEL_BG = "#FFFFFF"
CODE_PANEL_BORDER = "#E0DCD4"


def log(msg: str) -> None:
    print(msg, flush=True)


ZWSP_RE = re.compile(r"[\u200b\u200c\u200d\ufeff]")
ANCHOR_RE = re.compile(r"\s*\{#[^}]+\}\s*")


def clean_heading_text(text: str) -> str:
    text = ANCHOR_RE.sub(" ", text or "")
    text = ZWSP_RE.sub("", text)
    text = text.replace("\u00a0", " ")
    return re.sub(r"\s+", " ", text).strip()


def fence_tick_count(line: str) -> int:
    n = 0
    for ch in (line or "").lstrip():
        if ch == "`":
            n += 1
        else:
            break
    return n if n >= 3 else 0


class FenceTracker:
    def __init__(self) -> None:
        self.in_fence = False
        self.tick_len = 0

    def feed(self, line: str) -> bool:
        ticks = fence_tick_count(line)
        if not self.in_fence:
            if ticks >= 3:
                self.in_fence = True
                self.tick_len = ticks
                return True
            return False
        if ticks >= self.tick_len:
            self.in_fence = False
            self.tick_len = 0
            return True
        return False


ICON_CHARS: dict[str, str] = {
    "\u22ef": "three-dot",
    "\u22ee": "vertical three-dot",
    "\u2026": "...",
    "\u2190": "<-",
    "\u2191": "up",
    "\u2192": "->",
    "\u2193": "down",
    "\u2194": "<->",
    "\u21d2": "=>",
    "\u21bb": "retry",
    "\u21e7": "Shift",
    "\u2318": "Cmd",
    "\u2325": "Opt",
    "\u2605": "star",
    "\u2606": "star",
    "\u26a0": "warning",
    "\u270e": "edit",
    "\u2713": "check",
    "\u2714": "check",
    "\u2715": "X",
    "\u2716": "X",
    "\u2717": "X",
    "\u2718": "X",
    "\u274c": "X",
    "\u00d7": "X",
    "\u27f3": "retry",
    "\u2b06": "up",
    "\u2b07": "down",
    "\u00b7": "-",
    "\u2500": "-",
    "\u2502": "|",
    "\u250c": "+",
    "\u2510": "+",
    "\u2514": "+",
    "\u2518": "+",
    "\u251c": "+",
    "\u2524": "+",
    "\u252c": "+",
    "\u2534": "+",
    "\u253c": "+",
    "\U0001f418": "elephant",
    "\U0001f527": "wrench",
}

READABLE_CHARS: dict[str, str] = {
    **ICON_CHARS,
    "\u2039": "<",
    "\u203a": ">",
    "\u2122": "TM",
    "\u00a9": "(c)",
    "\u00ae": "(R)",
    "\u2013": "-",
    "\u2014": "--",
    "\u2018": "'",
    "\u2019": "'",
    "\u201c": '"',
    "\u201d": '"',
}


def readable_text(
    text: str,
    allowed: set[int] | None = None,
    *,
    icons_only: bool = False,
) -> str:
    if not text:
        return text
    catalog = ICON_CHARS if icons_only else READABLE_CHARS
    out: list[str] = []
    for i, ch in enumerate(text):
        code = ord(ch)
        if ch in "\n\t\r":
            out.append(ch)
            continue
        if code < 32:
            continue
        if allowed is None:
            keep = ch not in catalog
        else:
            keep = code in allowed
        if keep:
            out.append(ch)
            continue
        repl = catalog.get(ch)
        if repl is None:
            try:
                repl = unicodedata.name(ch).lower().replace("-", " ")
            except ValueError:
                repl = ""
        if not repl:
            continue
        prev = out[-1][-1] if out else ""
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if repl[0].isalnum() and prev and prev not in " \t\n([{\"'/":
            repl = " " + repl
        if repl[-1].isalnum() and nxt and nxt not in " \t\n.,;:)]}\"'/":
            repl = repl + " "
        out.append(repl)
    return re.sub(r"[ \t]{2,}", " ", "".join(out))


SENTENCE_SPLIT = re.compile(r'(?<=[.!?])[”"\']?\s+(?=[A-Z“"0-9])')


def tidy_line(line: str) -> str:
    stripped = line.strip()
    if not stripped or stripped.startswith("```") or stripped.startswith("|"):
        return line
    if set(stripped) <= {"-"} and len(stripped) >= 3:
        return line
    lead = line[: len(line) - len(line.lstrip())]
    if lead and stripped.startswith(("- ", "* ")):
        lead = "    "
    body = stripped
    body = re.sub(r"(?:\s+[—–]){2,}\s+", " — ", body)
    body = re.sub(r"\s+[—–]\s+-\s+", " — ", body)
    body = re.sub(r"\s+[—–]\s*:\s*[—–]?\s*", " — ", body)
    body = re.sub(r"\s+[—–]\s*,\s*[—–]?\s*", ", ", body)
    body = re.sub(r"\s+—\s+\(", " (", body)
    body = re.sub(r"\(\s+[—–]\s+", "(", body)
    body = re.sub(r"\s+[—–]\s+\)", ")", body)
    body = re.sub(r"\s+[—–]\s+'s\b", "'s", body)
    body = re.sub(r"™(?=[A-Za-z])", "™ ", body)
    body = re.sub(r"\s{2,}([.,;:])", r"\1", body)
    body = re.sub(r"\s+or\s+[—–]\s+(\[)", r" or \1", body)
    body = re.sub(r"\s+[—–]\s+([^—–\n]{1,40}?)\s*→\s*$", r" (\1)", body)
    body = re.sub(r"\s*→\s*$", "", body)
    body = re.sub(r"[ \t]{2,}", " ", body)
    return lead + body


def bold_list_term(line: str) -> str:
    m = re.match(r"^([-*] )(?!\*\*|\[)([^—\n]{1,70}?)( — )", line)
    if not m:
        return line
    name = m.group(2).strip()
    if name.startswith("`") or name.startswith("<"):
        return line
    return f"{m.group(1)}**{name}**{m.group(3)}{line[m.end():]}"


def looks_like_orphan_heading(line: str) -> bool:
    s = line.strip()
    if not s or s.startswith(("#", "-", "*", ">", "|", "!", "[", "`", "1", "2", "3", "4", "5", "6", "7", "8", "9")):
        return False
    if s.endswith((".", ":", "?", "!", ",", ";", "—")):
        return False
    if " — " in s or "http" in s or "**" in s:
        return False
    words = s.split()
    if not (1 <= len(words) <= 6) or len(s) > 52:
        return False
    if len(words) == 1 and (len(s) < 8 or not s.isalpha()):
        return False
    if words[0].lower() in {
        "welcome", "this", "that", "these", "those", "it", "you", "we",
        "a", "an", "the", "if", "when", "after", "before", "using",
        "already", "there", "here",
    }:
        return False
    if re.search(r"\b(is|are|was|were|has|have|will|can|cannot|don't|lets)\b", s, re.I):
        return False
    return words[0][:1].isupper()


def split_figure_note_body(prefix: str, body: str) -> list[str]:
    body = body.strip()
    if not body:
        return [prefix]
    parts = SENTENCE_SPLIT.split(body)
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) <= 1 and body.count("; ") >= 2:
        parts = [p.strip().rstrip(";") for p in body.split("; ") if p.strip()]
    if len(parts) <= 1:
        return [f"{prefix} {body}"]
    return [f"{prefix} {parts[0]}"] + parts[1:]


def _note_continues(line: str) -> bool:
    if not line.strip():
        return False
    s = line.lstrip()
    return not (
        s.startswith("#")
        or s.startswith("![")
        or s.startswith("---")
        or s.startswith("```")
        or s.startswith("|")
        or s.startswith("**Figure note.**")
        or s.startswith("_Source")
        or s.startswith("_Video")
        or re.match(r"^[-*]\s+", s)
        or re.match(r"^\d+\.\s+", s)
    )


def _is_block_start(line: str) -> bool:
    s = line.strip()
    if not s:
        return True
    if s.startswith(
        ("#", "```", "---", "|", ">", "![", "- ", "* ", "**Figure note.**", "_Source:", "_Video", "_Captured")
    ):
        return True
    if re.match(r"^\d+\.\s+", s):
        return True
    if re.match(r"^\s+[-*]\s+", line):
        return True
    return False


def unwrap_soft_breaks(lines: list[str]) -> list[str]:
    out: list[str] = []
    fences = FenceTracker()
    in_note = False
    buf: list[str] = []

    def flush() -> None:
        if buf:
            out.append(" ".join(part.strip() for part in buf))
            buf.clear()

    for ln in lines:
        if fences.feed(ln):
            flush()
            in_note = False
            out.append(ln)
            continue
        if fences.in_fence:
            out.append(ln)
            continue
        if not ln.strip():
            flush()
            in_note = False
            out.append(ln)
            continue
        if ln.lstrip().startswith("**Figure note.**"):
            flush()
            in_note = True
            out.append(ln)
            continue
        if in_note:
            flush()
            out.append(ln)
            continue
        if _is_block_start(ln):
            flush()
            out.append(ln)
            continue
        if (
            out
            and not buf
            and re.match(r"^(\s*[-*] |\s*\d+\.\s+)", out[-1])
            and "```" not in ln
            and "```" not in out[-1]
        ):
            out[-1] = out[-1].rstrip() + " " + ln.strip()
            continue
        buf.append(ln)
    flush()
    return out


def format_for_reading(md: str) -> str:
    raw_lines = md.splitlines()
    lines: list[str] = []
    fences = FenceTracker()
    for ln in raw_lines:
        if fences.feed(ln):
            lines.append(ln)
            continue
        if fences.in_fence:
            lines.append(ln)
            continue
        cleaned = tidy_line(ln)
        if cleaned.strip() and not cleaned.lstrip().startswith("#"):
            cleaned = readable_text(cleaned, icons_only=True)
        if re.match(r"^[-*] ", cleaned):
            cleaned = bold_list_term(cleaned)
        lines.append(cleaned)
    out: list[str] = []
    i = 0
    fences = FenceTracker()
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith("**Figure note.**"):
            block = [line]
            i += 1
            while i < len(lines) and _note_continues(lines[i]):
                block.append(lines[i])
                i += 1
            first = block[0].lstrip()
            m = re.match(r"^(\*\*Figure note\.\*\*)\s*(.*)$", first)
            if m and len(block) == 1:
                formatted = split_figure_note_body(m.group(1), m.group(2))
            else:
                formatted = [first] + [b.strip() for b in block[1:] if b.strip()]
            formatted = [readable_text(x, icons_only=True) for x in formatted]
            if out and out[-1].strip():
                out.append("")
            out.extend(formatted)
            out.append("")
            continue
        if fences.feed(line):
            out.append(line)
            i += 1
            continue
        if fences.in_fence:
            out.append(line)
            i += 1
            continue
        last_real = next((x for x in reversed(out) if x.strip()), "")
        past_front = any(x.startswith("## ") for x in out)
        if (
            past_front
            and looks_like_orphan_heading(line)
            and not last_real.startswith("#")
        ):
            out.append(f"### {line.strip()}")
            i += 1
            continue
        out.append(line)
        i += 1
    out = unwrap_soft_breaks(out)
    body = re.sub(r"\n{3,}", "\n\n", "\n".join(out))
    if md.endswith("\n") and not body.endswith("\n"):
        body += "\n"
    return body


def sanitize_figure_notes(md: str) -> str:
    return format_for_reading(md)


def attach_figure_notes(md: str) -> str:
    img_re = re.compile(r"!\[(.*?)\]\((images/[^)]+)\)")
    lines = md.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        m = img_re.search(line)
        if m:
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines) and lines[j].lstrip().startswith("**Figure note.**"):
                i += 1
                continue
            alt = (m.group(1) or "").strip()
            if alt:
                out.append("")
                out.append(f"**Figure note.** {alt}")
        i += 1
    return "\n".join(out) + "\n"


def compact_markdown(text: str) -> str:
    text = ZWSP_RE.sub("", text)
    out: list[str] = []
    last_h1 = ""
    for line in text.splitlines():
        if line.startswith("#"):
            hashes, _, rest = line.partition(" ")
            if set(hashes) == {"#"}:
                cleaned = clean_heading_text(rest)
                if hashes == "#" and cleaned.lower() == last_h1:
                    continue
                line = f"{hashes} {cleaned}".rstrip()
                if hashes == "#":
                    last_h1 = cleaned.lower()
        out.append(line.rstrip())
    joined = "\n".join(out)
    joined = re.sub(r"\n{3,}", "\n\n", joined)
    return sanitize_figure_notes(attach_figure_notes(joined.strip() + "\n"))


def slugify(text: str, fallback: str = "youtube-guide") -> str:
    text = unescape(text or "").strip().lower()
    text = re.sub(r"['’]", "", text)
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:80] or fallback


def format_timestamp(seconds: float | int) -> str:
    seconds = int(round(seconds))
    hrs = seconds // 3600
    mins = (seconds % 3600) // 60
    secs = seconds % 60
    if hrs > 0:
        return f"{hrs:02d}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


def parse_timestamp_str(s: str) -> int | None:
    s = s.strip()
    if not s:
        return None
    parts = s.split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(float(parts[2]))
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(float(parts[1]))
        if len(parts) == 1:
            return int(float(parts[0]))
    except ValueError:
        return None
    return None


def extract_video_id(url_or_id: str) -> str | None:
    text = url_or_id.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", text):
        return text
    m = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", text)
    if m:
        return m.group(1)
    m = re.search(r"youtu\.be/([A-Za-z0-9_-]{11})", text)
    if m:
        return m.group(1)
    m = re.search(r"youtube\.com/(?:embed|shorts|v)/([A-Za-z0-9_-]{11})", text)
    if m:
        return m.group(1)
    return None


# -----------------------------------------------------------------------------
# YouTube Transcript & Chapters Processing
# -----------------------------------------------------------------------------

def parse_description_chapters(description: str, video_duration: float) -> list[dict]:
    """Parse timestamp chapter lines in video description like '02:15 Installing'."""
    lines = description.splitlines()
    found: list[tuple[int, str]] = []
    ts_re = re.compile(r"^(?:[-*]\s*)?\(?(\d{1,2}:\d{2}(?::\d{2})?)\)?\s*(?:[-–—:|]\s*)?(.*)$")
    for ln in lines:
        ln_s = ln.strip()
        m = ts_re.match(ln_s)
        if m:
            sec = parse_timestamp_str(m.group(1))
            title = m.group(2).strip()
            if sec is not None and title:
                title = re.sub(r"\s*[-–—:|]\s*$", "", title).strip()
                found.append((sec, title))

    found.sort(key=lambda x: x[0])
    chapters: list[dict] = []
    for i, (sec, title) in enumerate(found):
        end_sec = found[i + 1][0] if i + 1 < len(found) else video_duration
        chapters.append({
            "title": title,
            "start_time": float(sec),
            "end_time": float(end_sec),
        })
    return chapters


def auto_segment_video(duration: float, segment_duration: int = 180) -> list[dict]:
    """Fallback: segment video into logical time chunks if no chapters exist."""
    chapters = []
    cur = 0.0
    idx = 1
    while cur < duration:
        end = min(cur + segment_duration, duration)
        title = f"Part {idx}: {format_timestamp(cur)} - {format_timestamp(end)}"
        chapters.append({
            "title": title,
            "start_time": cur,
            "end_time": end,
        })
        cur = end
        idx += 1
    return chapters


def fetch_youtube_metadata(video_url: str) -> dict:
    """Fetch video info via yt-dlp."""
    ydl_opts = {
        "extract_flat": False,
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "extractor_args": {"youtube": {"player_client": ["web", "ios", "android"]}},
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(video_url, download=False)
    return info


def fetch_transcript(video_id: str, preferred_lang: str = "en", meta: dict | None = None) -> list[dict]:
    """Fetch subtitles with timestamps using multiple fallback approaches."""
    # 1. Try from yt-dlp metadata captions if available
    if meta:
        caps_dict = meta.get("subtitles") or {}
        auto_caps = meta.get("automatic_captions") or {}
        
        # Search in subtitles first, then automatic captions
        for caps in [caps_dict, auto_caps]:
            matching_langs = [k for k in caps.keys() if k == preferred_lang or k.startswith(preferred_lang) or k == "en"]
            for lang_key in matching_langs:
                formats = caps.get(lang_key, [])
                # Prefer json3 or srv3 or ttml or vtt
                json3_fmt = next((f for f in formats if f.get("ext") == "json3"), None)
                if json3_fmt and json3_fmt.get("url"):
                    try:
                        r = requests.get(json3_fmt["url"], timeout=15)
                        if r.status_code == 200:
                            data = r.json()
                            cues = []
                            for ev in data.get("events", []):
                                segs = ev.get("segs", [])
                                text = "".join(s.get("utf8", "") for s in segs)
                                start = float(ev.get("tStartMs", 0)) / 1000.0
                                dur = float(ev.get("dDurationMs", 2000)) / 1000.0
                                if text.strip():
                                    cues.append({"text": text.strip(), "start": start, "duration": dur})
                            if cues:
                                return cues
                    except Exception as e:
                        log(f"json3 caption parsing note: {e}")

                ttml_fmt = next((f for f in formats if f.get("ext") in ("ttml", "srv1")), None)
                if ttml_fmt and ttml_fmt.get("url"):
                    try:
                        r = requests.get(ttml_fmt["url"], timeout=15)
                        if r.status_code == 200:
                            root = ET.fromstring(r.text)
                            cues = []
                            for node in root.findall(".//text"):
                                text = node.text or ""
                                start = float(node.attrib.get("start", 0.0))
                                dur = float(node.attrib.get("dur", 2.0))
                                if text.strip():
                                    cues.append({"text": text.strip(), "start": start, "duration": dur})
                            if cues:
                                return cues
                    except Exception as e:
                        log(f"ttml caption parsing note: {e}")

    # 2. Try youtube_transcript_api if available
    if YouTubeTranscriptApi:
        try:
            api = YouTubeTranscriptApi()
            trans = api.fetch(video_id, languages=[preferred_lang, "en", "en-US", "en-GB"])
            if trans:
                return [{"text": c.text, "start": float(c.start), "duration": float(c.duration)} for c in trans]
        except Exception as exc:
            log(f"youtube_transcript_api note: {exc}")

    # 3. Try scraping ytInitialPlayerResponse from watch page
    try:
        watch_url = f"https://www.youtube.com/watch?v={video_id}"
        headers = {
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        }
        r = requests.get(watch_url, headers=headers, timeout=20)
        if r.ok:
            m = re.search(r"ytInitialPlayerResponse\s*=\s*({.+?});", r.text)
            if not m:
                m = re.search(r"var ytInitialPlayerResponse\s*=\s*({.+?});", r.text)
            if m:
                data = json.loads(m.group(1))
                tracks = data.get("captions", {}).get("playerCaptionsTracklistRenderer", {}).get("captionTracks", [])
                for t in tracks:
                    b_url = t.get("baseUrl")
                    if b_url:
                        cap_r = requests.get(b_url, headers=headers, timeout=15)
                        if cap_r.status_code == 200:
                            root = ET.fromstring(cap_r.text)
                            cues = []
                            for node in root.findall(".//text"):
                                text = node.text or ""
                                start = float(node.attrib.get("start", 0.0))
                                dur = float(node.attrib.get("dur", 2.0))
                                if text.strip():
                                    cues.append({"text": text.strip(), "start": start, "duration": dur})
                            if cues:
                                return cues
    except Exception as exc:
        log(f"Watch page caption extraction note: {exc}")

    return []


def clean_transcript_cues(cues: list[dict]) -> list[dict]:
    """Remove music tags, normalize spaces, deduplicate rapid repeated subtitles."""
    cleaned = []
    last_text = ""
    for c in cues:
        t = c["text"]
        t = re.sub(r"\[(Music|Applause|Laughter|Laughter and applause|Silence)\]", "", t, flags=re.I)
        t = unescape(t).strip()
        t = re.sub(r"\s+", " ", t)
        if not t or t.lower() == last_text.lower():
            continue
        last_text = t
        cleaned.append({
            "text": t,
            "start": c["start"],
            "duration": c.get("duration", 0.0),
        })
    return cleaned


def group_cues_into_paragraphs(cues: list[dict], pause_threshold: float = 2.0) -> list[str]:
    """Group sequential subtitle lines into flowing prose paragraphs."""
    if not cues:
        return []
    paragraphs: list[str] = []
    current_para: list[str] = []
    last_end = 0.0

    for cue in cues:
        start = cue["start"]
        dur = cue.get("duration", 2.0)
        text = cue["text"]

        if current_para and (start - last_end > pause_threshold or len(" ".join(current_para)) > 450):
            p_text = " ".join(current_para).strip()
            if p_text:
                paragraphs.append(p_text)
            current_para = []

        current_para.append(text)
        last_end = start + dur

    if current_para:
        p_text = " ".join(current_para).strip()
        if p_text:
            paragraphs.append(p_text)

    formatted_paras = []
    for p in paragraphs:
        if p and p[0].islower():
            p = p[0].upper() + p[1:]
        if p and not p.endswith((".", "!", "?")):
            p = p + "."
        formatted_paras.append(p)

    return formatted_paras


# -----------------------------------------------------------------------------
# Video Frame Extraction via yt-dlp and ffmpeg
# -----------------------------------------------------------------------------

def extract_frames_for_timestamps(
    video_url: str,
    timestamps: list[tuple[float, str]],  # (seconds, output_filename_stem)
    img_dir: Path,
    resolution: int = 720,
) -> dict[float, Path]:
    """Download video stream locally or seek with ffmpeg to extract crisp frame snapshots."""
    img_dir.mkdir(parents=True, exist_ok=True)
    out_map: dict[float, Path] = {}
    if not timestamps:
        return out_map

    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        log("Warning: ffmpeg not found on PATH. Cannot extract still frames.")
        return out_map

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_video = Path(tmp_dir) / "video_stream.mp4"
        log(f"Fetching video stream ({resolution}p) for frame extraction...")
        ydl_opts = {
            "format": f"bestvideo[height<={resolution}][ext=mp4]+bestaudio[ext=m4a]/best[height<={resolution}]/best",
            "outtmpl": str(tmp_video),
            "quiet": True,
            "no_warnings": True,
            "extractor_args": {"youtube": {"player_client": ["web", "ios", "android"]}},
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([video_url])
        except Exception as exc:
            log(f"yt-dlp download note: {exc}")

        video_src = tmp_video if tmp_video.is_file() and tmp_video.stat().st_size > 0 else None
        if not video_src:
            try:
                with yt_dlp.YoutubeDL({"quiet": True, "extractor_args": {"youtube": {"player_client": ["web", "ios", "android"]}}}) as ydl:
                    info = ydl.extract_info(video_url, download=False)
                    video_src = info.get("url")
            except Exception:
                video_src = None

        if not video_src:
            log("Could not obtain video source for ffmpeg.")
            return out_map

        for sec, stem in timestamps:
            out_file = img_dir / f"{stem}.jpg"
            ts_formatted = format_timestamp(sec)
            cmd = [
                ffmpeg_bin,
                "-ss", str(sec),
                "-i", str(video_src),
                "-frames:v", "1",
                "-q:v", "2",
                "-y",
                str(out_file),
            ]
            try:
                res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=45)
                if res.returncode == 0 and out_file.is_file() and out_file.stat().st_size > 0:
                    out_map[sec] = out_file
                    log(f"  [Frame] {out_file.name} (at {ts_formatted})")
                else:
                    log(f"  [Frame Failed] at {ts_formatted}")
            except Exception as e:
                log(f"  ffmpeg error at {ts_formatted}: {e}")

    return out_map


# -----------------------------------------------------------------------------
# PDF Engine & Typography (ReportLab Book Standard)
# -----------------------------------------------------------------------------

BODY_PT = 10.5
HEADING_RATIO = {"h1": 1.75, "h2": 1.32, "h3": 1.16}


def _cap_units(face_name: str) -> float:
    from reportlab.pdfbase import pdfmetrics
    try:
        face = pdfmetrics.getFont(face_name).face
    except Exception:
        return 700.0
    cap = float(getattr(face, "capHeight", 0) or 0)
    if cap > 0:
        return cap
    return float(getattr(face, "ascent", 700) or 700)


def _condense_boost(body_face: str, head_face: str) -> float:
    from reportlab.pdfbase import pdfmetrics
    try:
        body_w = pdfmetrics.stringWidth("H", body_face, 1000) or 1.0
        head_w = pdfmetrics.stringWidth("H", head_face, 1000) or body_w
    except Exception:
        return 1.0
    ratio = body_w / head_w
    if ratio <= 1.08:
        return 1.0
    return min(1.35, 1.0 + (ratio - 1.0) * 0.45)


def heading_pt(body_pt: float, body_face: str, head_face: str, ratio: float) -> float:
    cap_scale = _cap_units(body_face) / (_cap_units(head_face) or 1.0)
    pt = body_pt * ratio * cap_scale * _condense_boost(body_face, head_face)
    return round(max(pt, body_pt + 1.5), 1)


def heading_scale(faces: dict[str, str], body_pt: float = BODY_PT) -> dict[str, float]:
    h1 = heading_pt(body_pt, faces["body"], faces["h1"], HEADING_RATIO["h1"])
    h2 = heading_pt(body_pt, faces["body"], faces["h2"], HEADING_RATIO["h2"])
    h3 = heading_pt(body_pt, faces["body"], faces["h2"], HEADING_RATIO["h3"])
    h2 = min(h2, h1 - 1.5)
    h3 = min(h3, h2 - 1.0)
    h3 = max(h3, body_pt + 1.5)
    h2 = max(h2, h3 + 1.0)
    h1 = max(h1, h2 + 1.5)
    return {"h1": h1, "h2": h2, "h3": h3, "body": body_pt}


def _font_codepoints(face_name: str) -> set[int]:
    allowed = set(range(32, 127))
    try:
        from reportlab.pdfbase import pdfmetrics
        font = pdfmetrics.getFont(face_name)
        widths = getattr(getattr(font, "face", None), "charWidths", None)
        if widths:
            allowed.update(int(k) for k in widths if isinstance(k, int) and k < 0x110000)
    except Exception:
        pass
    return allowed


def _register_one_font(label: str, path: Path | str | None, fallback: str) -> str:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    if not path:
        return fallback
    p = Path(path)
    if not p.exists():
        return fallback
    face = f"YT-{label}"
    try:
        pdfmetrics.registerFont(TTFont(face, str(p)))
        return face
    except Exception:
        return fallback


def _register_guide_fonts(spec: dict[str, str] | None = None) -> dict[str, str]:
    spec = spec or {}
    mapping = {
        "h1": _register_one_font("h1", spec.get("h1"), "Helvetica-Bold"),
        "h2": _register_one_font("h2", spec.get("h2"), "Helvetica-Bold"),
        "body": _register_one_font("body", spec.get("body"), "Helvetica"),
        "body-bold": _register_one_font("body-bold", spec.get("body_bold"), "Helvetica-Bold"),
        "body-italic": _register_one_font("body-italic", spec.get("body_italic"), "Helvetica-Oblique"),
        "code": _register_one_font("code", spec.get("code"), "Courier"),
    }
    if not spec:
        fonts_dir = Path(r"C:\Windows\Fonts")
        mapping["body"] = _register_one_font("body", fonts_dir / "segoeui.ttf", mapping["body"])
        mapping["body-bold"] = _register_one_font("body-bold", fonts_dir / "segoeuib.ttf", mapping["body-bold"])
        mapping["body-italic"] = _register_one_font("body-italic", fonts_dir / "segoeuii.ttf", mapping["body-italic"])
        mapping["h1"] = _register_one_font("h1", fonts_dir / "segoeuib.ttf", mapping["h1"])
        mapping["h2"] = _register_one_font("h2", fonts_dir / "segoeuib.ttf", mapping["h2"])
        mapping["code"] = _register_one_font("code", fonts_dir / "consola.ttf", mapping["code"])
    try:
        from reportlab.pdfbase.pdfmetrics import registerFontFamily
        registerFontFamily(
            mapping["body"],
            normal=mapping["body"],
            bold=mapping["body-bold"],
            italic=mapping["body-italic"],
            boldItalic=mapping["body-bold"],
        )
    except Exception:
        pass
    return mapping


def _flow_style(flow) -> str:
    return getattr(getattr(flow, "style", None), "name", "") or ""


def keep_headings_with_body(story: list) -> list:
    from reportlab.lib.units import inch
    from reportlab.platypus import CondPageBreak, KeepTogether, Spacer
    heading_names = {"H1g", "H2g", "H3g"}
    out: list = []
    i = 0
    while i < len(story):
        if _flow_style(story[i]) not in heading_names:
            out.append(story[i])
            i += 1
            continue
        level = _flow_style(story[i])
        chunk: list = []
        while i < len(story) and _flow_style(story[i]) in heading_names | {"Cap"}:
            chunk.append(story[i])
            i += 1
        if i < len(story) and isinstance(story[i], Spacer):
            height = float(getattr(story[i], "height", 0) or 0)
            if height <= 8:
                i += 1
        took_figure = False
        if i < len(story) and _flow_style(story[i]) not in heading_names:
            chunk.append(story[i])
            kind = type(story[i]).__name__
            took_figure = kind in {"KeepTogether", "Image", "Table"} or _flow_style(story[i]) == "FigBody"
            i += 1
            if _flow_style(chunk[-1]) in {"GBullet", "GBulletNest"}:
                extra = 0
                while (
                    i < len(story)
                    and extra < 2
                    and _flow_style(story[i]) in {"GBullet", "GBulletNest"}
                ):
                    chunk.append(story[i])
                    i += 1
                    extra += 1
            if (
                not took_figure
                and _flow_style(chunk[-1]) in {"Body", "Cap"}
                and i < len(story)
                and type(story[i]).__name__ == "KeepTogether"
            ):
                chunk.append(story[i])
                took_figure = True
                i += 1
        need = {"H1g": 2.1, "H2g": 1.6, "H3g": 1.3}.get(level, 1.3)
        if took_figure:
            need = max(need, 3.1)
        out.append(CondPageBreak(need * inch))
        out.append(KeepTogether(chunk) if len(chunk) > 1 else chunk[0])
    return out


def highlight_code_pygments(code_text: str, lang: str = "") -> list[tuple[str, str]]:
    """Return list of (hex_color, token_text)."""
    try:
        import pygments
        from pygments.lexers import get_lexer_by_name, guess_lexer
        from pygments.token import Token
    except ImportError:
        return [(CODE_COLOR_DEFAULT, code_text)]

    lexer = None
    if lang:
        try:
            lexer = get_lexer_by_name(lang)
        except Exception:
            pass
    if not lexer:
        try:
            lexer = guess_lexer(code_text)
        except Exception:
            pass

    if not lexer:
        return [(CODE_COLOR_DEFAULT, code_text)]

    tokens = pygments.lex(code_text, lexer)
    colored_runs = []
    for ttype, tval in tokens:
        if not tval:
            continue
        color = CODE_COLOR_DEFAULT
        if ttype in Token.Keyword:
            color = CODE_COLOR_KEYWORD
        elif ttype in Token.Literal.String:
            color = CODE_COLOR_STRING
        elif ttype in Token.Comment:
            color = CODE_COLOR_COMMENT
        elif ttype in (Token.Name.Class, Token.Name.Function, Token.Name.Builtin, Token.Literal.Number):
            color = CODE_COLOR_NAME
        colored_runs.append((color, tval))
    return colored_runs


def write_pdf(
    md_path: Path,
    pdf_path: Path,
    image_dir: Path,
    font_spec: dict[str, str] | None = None,
) -> None:
    from reportlab.lib.colors import HexColor
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.lib.utils import ImageReader
    from reportlab.platypus import (
        Image,
        KeepTogether,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    faces = _register_guide_fonts(font_spec)
    scale = heading_scale(faces)
    log(
        f"Type scale: body {scale['body']} / h3 {scale['h3']} / "
        f"h2 {scale['h2']} / h1 {scale['h1']}"
    )
    raw_md = md_path.read_text(encoding="utf-8")
    cleaned_md = sanitize_figure_notes(raw_md)
    if cleaned_md != raw_md:
        md_path.write_text(cleaned_md, encoding="utf-8")
        log("Reformatted markdown for reading (notes, spacing, list punctuation)")

    face_cps = {role: _font_codepoints(name) for role, name in faces.items()}
    ink = HexColor("#1a1714")
    muted = HexColor("#6b635b")
    heading = HexColor("#2a1810")
    note_bg = HexColor("#f3eee4")
    note_edge = HexColor("#c9b89a")
    fig_body = HexColor("#3f3a34")

    styles = getSampleStyleSheet()
    styles.add(
        ParagraphStyle(
            name="Body",
            fontName=faces["body"],
            fontSize=scale["body"],
            leading=round(scale["body"] * 1.38, 1),
            textColor=ink,
            spaceAfter=5,
        )
    )
    styles.add(
        ParagraphStyle(
            name="CoverTitle",
            fontName=faces["h1"],
            fontSize=26,
            leading=30,
            textColor=heading,
            spaceBefore=20,
            spaceAfter=8,
        )
    )
    styles.add(
        ParagraphStyle(
            name="H1g",
            fontName=faces["h1"],
            fontSize=scale["h1"],
            leading=round(scale["h1"] * 1.18, 1),
            textColor=heading,
            spaceBefore=14,
            spaceAfter=6,
            keepWithNext=1,
        )
    )
    styles.add(
        ParagraphStyle(
            name="H2g",
            fontName=faces["h2"],
            fontSize=scale["h2"],
            leading=round(scale["h2"] * 1.22, 1),
            textColor=heading,
            spaceBefore=10,
            spaceAfter=4,
            keepWithNext=1,
        )
    )
    styles.add(
        ParagraphStyle(
            name="H3g",
            fontName=faces["h2"],
            fontSize=scale["h3"],
            leading=round(scale["h3"] * 1.24, 1),
            textColor=heading,
            spaceBefore=8,
            spaceAfter=2,
            keepWithNext=1,
        )
    )
    styles.add(
        ParagraphStyle(
            name="Cap",
            fontName=faces["body-italic"],
            fontSize=8.5,
            leading=11.5,
            textColor=muted,
            spaceBefore=2,
            spaceAfter=3,
            keepWithNext=1,
        )
    )
    styles.add(
        ParagraphStyle(
            name="FigBody",
            fontName=faces["body"],
            fontSize=8.5,
            leading=11.5,
            textColor=fig_body,
            spaceAfter=0,
        )
    )
    styles.add(
        ParagraphStyle(
            name="GBullet",
            fontName=faces["body"],
            fontSize=scale["body"],
            leading=round(scale["body"] * 1.40, 1),
            textColor=ink,
            leftIndent=16,
            firstLineIndent=-12,
            spaceBefore=1,
            spaceAfter=3,
        )
    )
    styles.add(
        ParagraphStyle(
            name="Toc",
            fontName=faces["body"],
            fontSize=9.5,
            leading=13,
            textColor=ink,
            spaceAfter=0,
        )
    )

    margin = 0.7 * inch
    page_w, page_h = letter
    usable_w = page_w - 2 * margin
    usable_h = page_h - 2 * margin - 16

    def esc(s: str) -> str:
        s = ZWSP_RE.sub("", s)
        s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        codes: list[str] = []

        def _hold_code(m: re.Match) -> str:
            codes.append(m.group(1))
            return f"\x00CODE{len(codes) - 1}\x00"

        s = re.sub(r"`([^`]+)`", _hold_code, s)
        s = re.sub(r"\*\*\*(.+?)\*\*\*", r"<b><i>\1</i></b>", s)
        s = re.sub(r"___(.+?)___", r"<b><i>\1</i></b>", s)
        s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
        s = re.sub(r"(?<!\*)\*(.+?)\*(?!\*)", r"<i>\1</i>", s)
        s = re.sub(r"^_(.+)_$", r"<i>\1</i>", s)
        s = re.sub(
            r"\x00CODE(\d+)\x00",
            lambda m: f"<font name='{faces['code']}' size='8'>{codes[int(m.group(1))]}</font>",
            s,
        )
        s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
        return s

    def _fit(text: str, role: str) -> str:
        return readable_text(text, face_cps.get(role, face_cps["body"]))

    def note_table(text: str):
        parts = [p.strip() for p in text.splitlines() if p.strip()]
        if not parts:
            return Spacer(1, 1)
        try:
            joined = "<br/>".join(esc(_fit(p, "body")) for p in parts)
            cell = Paragraph(joined, styles["FigBody"])
        except Exception:
            plain = " ".join(re.sub(r"[*_`]+", "", p) for p in parts)
            cell = Paragraph(esc(plain[:2000]), styles["FigBody"])
        tbl = Table([[cell]], colWidths=[usable_w])
        tbl.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), note_bg),
                    ("BOX", (0, 0), (-1, -1), 0.5, note_edge),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]
            )
        )
        return tbl

    def code_panel(code_text: str, lang: str = ""):
        runs = highlight_code_pygments(code_text, lang)
        html_parts = []
        for color, val in runs:
            escaped_val = val.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            escaped_val = escaped_val.replace(" ", "&nbsp;").replace("\n", "<br/>")
            html_parts.append(f"<font color='{color}'>{escaped_val}</font>")

        para = Paragraph(f"<font name='{faces['code']}' size='8'>{''.join(html_parts)}</font>", styles["FigBody"])
        tbl = Table([[para]], colWidths=[usable_w])
        tbl.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), HexColor(CODE_PANEL_BG)),
                    ("BOX", (0, 0), (-1, -1), 0.5, HexColor(CODE_PANEL_BORDER)),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ]
            )
        )
        return tbl

    def toc_table(items: list[str]):
        mid = (len(items) + 1) // 2
        left, right = items[:mid], items[mid:]
        rows = []
        for i in range(max(len(left), len(right))):
            ltxt = f"{i + 1}. {left[i]}" if i < len(left) else ""
            rtxt = f"{mid + i + 1}. {right[i]}" if i < len(right) else ""
            rows.append(
                [
                    Paragraph(esc(_fit(ltxt, "body")), styles["Toc"]),
                    Paragraph(esc(_fit(rtxt, "body")), styles["Toc"]),
                ]
            )
        tbl = Table(rows, colWidths=[usable_w / 2.0, usable_w / 2.0])
        tbl.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                    ("TOPPADDING", (0, 0), (-1, -1), 1),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
                ]
            )
        )
        return tbl

    story: list = []
    lines = cleaned_md.splitlines()
    i = 0
    in_code = False
    code_buf: list[str] = []
    code_lang = ""
    toc_items: list[str] = []
    in_toc = False

    pending_fig: dict | None = None

    def flush_pending_fig(note: str | None = None) -> None:
        nonlocal pending_fig
        if not pending_fig:
            if note:
                story.append(note_table(note))
                story.append(Spacer(1, 6))
            return
        img_p = pending_fig["path"]
        alt_t = pending_fig["alt"]
        try:
            iw, ih = ImageReader(str(img_p)).getSize()
            if iw and ih:
                max_h = min(usable_h * 0.46, 3.55 * inch)
                scale_factor = min(usable_w / iw, max_h / ih, 1.0)
                disp_w = max(iw * scale_factor, 40)
                disp_h = max(ih * scale_factor, 40)
                img_flow = Image(str(img_p), width=disp_w, height=disp_h)
                img_flow.hAlign = "LEFT"
                bundle = [img_flow]
                if alt_t:
                    bundle.append(Paragraph(esc(_fit(alt_t, "body-italic")), styles["Cap"]))
                if note:
                    bundle.append(note_table(note))
                story.append(KeepTogether(bundle))
                story.append(Spacer(1, 6))
        except Exception as e:
            log(f"Image load error {img_p}: {e}")
        pending_fig = None

    while i < len(lines):
        line = lines[i]
        s = line.strip()

        if s.startswith("```"):
            if not in_code:
                flush_pending_fig()
                in_code = True
                code_lang = s[3:].strip()
                code_buf = []
            else:
                in_code = False
                joined_code = "\n".join(code_buf)
                story.append(code_panel(joined_code, code_lang))
                story.append(Spacer(1, 6))
                code_buf = []
            i += 1
            continue

        if in_code:
            code_buf.append(line)
            i += 1
            continue

        if not s:
            i += 1
            continue

        if line.lstrip().startswith("**Figure note.**"):
            note_lines = [line]
            i += 1
            while i < len(lines) and _note_continues(lines[i]):
                note_lines.append(lines[i])
                i += 1
            flush_pending_fig("\n".join(note_lines))
            continue

        m_img = re.search(r"!\[(.*?)\]\((images/[^)]+)\)", s)
        if m_img:
            flush_pending_fig()
            alt = m_img.group(1)
            img_rel = m_img.group(2)
            full_img_p = md_path.parent / img_rel
            if full_img_p.is_file():
                pending_fig = {"path": full_img_p, "alt": alt}
            i += 1
            continue

        flush_pending_fig()

        if s.startswith("# "):
            title_text = s[2:].strip()
            if not story:
                story.append(Paragraph(esc(_fit(title_text, "h1")), styles["CoverTitle"]))
            else:
                story.append(Paragraph(esc(_fit(title_text, "h1")), styles["H1g"]))
            i += 1
            continue

        if s.startswith("## "):
            h2_text = s[3:].strip()
            if h2_text.lower() == "contents":
                in_toc = True
                toc_items = []
                story.append(Paragraph(esc(_fit("Contents", "h2")), styles["H2g"]))
            else:
                if in_toc and toc_items:
                    story.append(toc_table(toc_items))
                    story.append(Spacer(1, 10))
                    in_toc = False
                story.append(Paragraph(esc(_fit(h2_text, "h2")), styles["H2g"]))
            i += 1
            continue

        if s.startswith("### "):
            story.append(Paragraph(esc(_fit(s[4:].strip(), "h2")), styles["H3g"]))
            i += 1
            continue

        if in_toc and re.match(r"^\d+\.\s+\[([^\]]+)\]", s):
            m_toc = re.match(r"^\d+\.\s+\[([^\]]+)\]", s)
            if m_toc:
                toc_items.append(m_toc.group(1))
            i += 1
            continue

        if in_toc and not re.match(r"^\d+\.\s+", s):
            if toc_items:
                story.append(toc_table(toc_items))
                story.append(Spacer(1, 10))
            in_toc = False

        if s.startswith("_Captured") or s.startswith("_Video") or s.startswith("_Source"):
            story.append(Paragraph(esc(_fit(s, "body-italic")), styles["Cap"]))
            i += 1
            continue

        if re.match(r"^[-*]\s+", s):
            bullet_text = re.sub(r"^[-*]\s+", "", s)
            story.append(Paragraph(esc(_fit(bullet_text, "body")), styles["GBullet"]))
            i += 1
            continue

        if re.match(r"^\d+\.\s+", s):
            num_text = re.sub(r"^\d+\.\s+", "", s)
            story.append(Paragraph(esc(_fit(num_text, "body")), styles["GBullet"]))
            i += 1
            continue

        story.append(Paragraph(esc(_fit(s, "body")), styles["Body"]))
        i += 1

    flush_pending_fig()
    if in_toc and toc_items:
        story.append(toc_table(toc_items))
        story.append(Spacer(1, 10))

    final_story = keep_headings_with_body(story)
    doc = SimpleDocTemplate(
        str(pdf_path),
        pagesize=letter,
        leftMargin=margin,
        rightMargin=margin,
        topMargin=margin,
        bottomMargin=margin,
    )
    doc.build(final_story)


# -----------------------------------------------------------------------------
# Main Capture Routine
# -----------------------------------------------------------------------------

def capture_youtube_video(
    video_input: str,
    out_dir_arg: str = "",
    format_opt: str = "both",
    shots_per_chapter: int = 2,
    interval_sec: int = 45,
    custom_timestamps: list[int] | None = None,
    resolution: int = 720,
    preferred_lang: str = "en",
    title_override: str = "",
    font_spec: dict[str, str] | None = None,
) -> int:
    video_id = extract_video_id(video_input)
    if not video_id:
        log(f"Invalid YouTube URL or video ID: {video_input}")
        return 2

    video_url = f"https://www.youtube.com/watch?v={video_id}"
    log(f"Processing YouTube video: {video_url}")

    try:
        meta = fetch_youtube_metadata(video_url)
    except Exception as exc:
        log(f"Failed to fetch YouTube metadata: {exc}")
        return 3

    video_title = title_override or meta.get("title") or f"YouTube Video {video_id}"
    channel = meta.get("uploader") or meta.get("channel") or "YouTube Creator"
    duration = float(meta.get("duration") or 0.0)
    description = meta.get("description") or ""
    thumbnail_url = meta.get("thumbnail")

    log(f"Title: {video_title}")
    log(f"Channel: {channel} | Duration: {format_timestamp(duration)}")

    raw_chapters = meta.get("chapters") or []
    if not raw_chapters:
        raw_chapters = parse_description_chapters(description, duration)
    if not raw_chapters:
        raw_chapters = auto_segment_video(duration, segment_duration=180)

    log(f"Detected {len(raw_chapters)} chapters/sections.")

    log(f"Fetching transcript for video {video_id}...")
    cues = fetch_transcript(video_id, preferred_lang=preferred_lang, meta=meta)
    cues = clean_transcript_cues(cues)
    log(f"Retrieved {len(cues)} transcript cues.")

    slug = slugify(video_title)
    out_dir = Path(out_dir_arg) if out_dir_arg else Path.home() / "Documents" / "guides" / slug
    img_dir = out_dir / "images"
    img_dir.mkdir(parents=True, exist_ok=True)

    cover_img_rel = None
    if thumbnail_url:
        try:
            r = requests.get(thumbnail_url, timeout=20)
            if r.ok:
                cover_path = img_dir / "00-cover-thumbnail.jpg"
                cover_path.write_bytes(r.content)
                cover_img_rel = f"images/{cover_path.name}"
                log(f"Downloaded thumbnail: {cover_img_rel}")
        except Exception as e:
            log(f"Thumbnail download failed: {e}")

    frame_targets: list[tuple[float, str]] = []
    if custom_timestamps:
        for idx, ts in enumerate(custom_timestamps, 1):
            stem = f"{idx:02d}-{format_timestamp(ts).replace(':', 'm')}s-custom"
            frame_targets.append((float(ts), stem))
    else:
        frame_idx = 1
        for ch_idx, ch in enumerate(raw_chapters, 1):
            ch_start = ch["start_time"]
            ch_end = ch["end_time"]
            ch_dur = max(ch_end - ch_start, 1.0)
            ch_slug = slugify(ch["title"])[:30]

            t1 = min(ch_start + 3.0, ch_end - 1.0)
            stem1 = f"{frame_idx:02d}-{format_timestamp(t1).replace(':', 'm')}s-{ch_slug}"
            frame_targets.append((t1, stem1))
            frame_idx += 1

            if shots_per_chapter > 1 and ch_dur > 25.0:
                t2 = ch_start + ch_dur * 0.55
                stem2 = f"{frame_idx:02d}-{format_timestamp(t2).replace(':', 'm')}s-{ch_slug}"
                frame_targets.append((t2, stem2))
                frame_idx += 1

    log(f"Extracting {len(frame_targets)} still frames from video...")
    captured_frames = extract_frames_for_timestamps(
        video_url=video_url,
        timestamps=frame_targets,
        img_dir=img_dir,
        resolution=resolution,
    )

    chapter_frames: list[list[tuple[float, Path]]] = [[] for _ in raw_chapters]
    for sec, path in captured_frames.items():
        assigned = False
        for idx, ch in enumerate(raw_chapters):
            if ch["start_time"] <= sec <= ch["end_time"]:
                chapter_frames[idx].append((sec, path))
                assigned = True
                break
        if not assigned and raw_chapters:
            chapter_frames[0].append((sec, path))

    chapter_cues: list[list[dict]] = [[] for _ in raw_chapters]
    for c in cues:
        c_time = c["start"]
        assigned = False
        for idx, ch in enumerate(raw_chapters):
            if ch["start_time"] <= c_time <= ch["end_time"]:
                chapter_cues[idx].append(c)
                assigned = True
                break
        if not assigned and raw_chapters:
            chapter_cues[-1].append(c)

    md_chunks: list[str] = [
        f"# {video_title}",
        "",
        f"_Captured {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} from [{channel} on YouTube]({video_url}). Duration: {format_timestamp(duration)}._",
        "",
    ]

    if cover_img_rel:
        md_chunks.append(f"![{video_title} - Video Cover]({cover_img_rel})")
        md_chunks.append(f"**Figure note.** Video thumbnail for '{video_title}' by {channel}.")
        md_chunks.append("")

    md_chunks.append("## Introduction")
    md_chunks.append("")
    intro_text = ""
    if description.strip():
        first_desc_lines = [ln.strip() for ln in description.splitlines() if ln.strip() and not ln.strip().startswith(("http", "0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "#", "@"))]
        if first_desc_lines:
            intro_text = " ".join(first_desc_lines[:3])

    if not intro_text and chapter_cues and chapter_cues[0]:
        intro_paras = group_cues_into_paragraphs(chapter_cues[0][:15])
        if intro_paras:
            intro_text = intro_paras[0]

    if not intro_text:
        intro_text = f"This guide covers the key steps, concepts, and demonstrations presented in '{video_title}' by {channel}."

    md_chunks.append(intro_text)
    md_chunks.append("")

    md_chunks.append("## Contents")
    md_chunks.append("")
    for i, ch in enumerate(raw_chapters, 1):
        ch_ts = format_timestamp(ch["start_time"])
        md_chunks.append(f"{i}. [{ch['title']} ({ch_ts})](#chapter-{i:02d})")
    md_chunks.append("")

    chapter_manifest: list[dict] = []
    for i, ch in enumerate(raw_chapters, 1):
        ch_title = clean_heading_text(ch["title"])
        ch_start = ch["start_time"]
        ch_start_sec = int(ch_start)
        ch_ts = format_timestamp(ch_start)
        yt_jump_url = f"{video_url}&t={ch_start_sec}s"

        md_chunks.append("\n---\n")
        md_chunks.append(f"## {i}. {ch_title} [{ch_ts}]\n")
        md_chunks.append(f"_Video timestamp: [{ch_ts}]({yt_jump_url})_\n")

        cues_in_ch = chapter_cues[i - 1]
        paras = group_cues_into_paragraphs(cues_in_ch)
        
        frames_in_ch = chapter_frames[i - 1]
        frames_in_ch.sort(key=lambda x: x[0])

        if paras:
            md_chunks.append(paras[0])
            md_chunks.append("")

        for f_sec, f_path in frames_in_ch:
            rel_f = f"images/{f_path.name}"
            f_ts = format_timestamp(f_sec)
            md_chunks.append(f"![{ch_title} at {f_ts}]({rel_f})")
            md_chunks.append(
                f"**Figure note.** Video frame at {f_ts} during '{ch_title}'.\n"
                f"Visual: Still screenshot showing the visual state, UI, or demonstration at this timestamp."
            )
            md_chunks.append("")

        if len(paras) > 1:
            for p in paras[1:]:
                md_chunks.append(p)
                md_chunks.append("")

        chapter_manifest.append({
            "index": i,
            "title": ch_title,
            "start_time": ch_start,
            "timestamp": ch_ts,
            "video_url": yt_jump_url,
            "images": [f"images/{p.name}" for _, p in frames_in_ch],
            "transcript_paragraphs": len(paras),
        })

    md_chunks.append("\n---\n")
    md_chunks.append("## Sources")
    md_chunks.append("")
    md_chunks.append(f"1. **{video_title}** — [{video_url}]({video_url})")
    md_chunks.append(f"2. **Creator Channel** — {channel}")
    md_chunks.append(f"3. **Transcripts** — YouTube Captions ({preferred_lang})")
    md_chunks.append("")

    md_path = out_dir / "guide.md"
    full_md = compact_markdown("\n".join(md_chunks))
    md_path.write_text(full_md, encoding="utf-8")
    log(f"Wrote markdown guide: {md_path}")

    pdf_path = None
    if format_opt in {"pdf", "both"}:
        pdf_path = out_dir / "guide.pdf"
        try:
            write_pdf(md_path, pdf_path, img_dir, font_spec)
            log(f"Wrote publication PDF: {pdf_path}")
        except Exception as exc:
            log(f"PDF generation failed: {exc}")
            pdf_path = None

    manifest = {
        "title": video_title,
        "video_id": video_id,
        "url": video_url,
        "channel": channel,
        "duration": duration,
        "duration_formatted": format_timestamp(duration),
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "chapter_count": len(raw_chapters),
        "frame_count": len(captured_frames),
        "chapters": chapter_manifest,
        "files": {
            "markdown": "guide.md",
            "pdf": "guide.pdf" if pdf_path else None,
        },
    }
    man_path = out_dir / "manifest.json"
    man_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    log(f"Wrote manifest: {man_path}")

    log(f"Done! Guide generated: {len(raw_chapters)} chapters, {len(captured_frames)} still frames -> {out_dir}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Capture a YouTube video tutorial as local markdown + still images + PDF guide.")
    ap.add_argument("url", nargs="?", default="", help="YouTube video URL or Video ID (omit with --from-md).")
    ap.add_argument(
        "--from-md",
        default="",
        help="Rebuild guide.pdf from an existing guide.md (no crawl/video download).",
    )
    ap.add_argument("--out", default="", help="Output directory path (defaults to ~/Documents/guides/<slug>).")
    ap.add_argument("--format", choices=("md", "pdf", "both"), default="both", help="Output format.")
    ap.add_argument("--shots-per-chapter", type=int, default=2, help="Number of still frames to extract per chapter.")
    ap.add_argument("--interval", type=int, default=45, help="Interval in seconds for still frame sampling.")
    ap.add_argument("--timestamps", default="", help="Comma-separated timestamps for still frame extraction (e.g. 0:30,2:15,10:00).")
    ap.add_argument("--resolution", type=int, default=720, help="Video resolution height for screenshot capture (default: 720).")
    ap.add_argument("--lang", default="en", help="Subtitle/transcript language code (default: en).")
    ap.add_argument("--title", default="", help="Override guide title.")
    ap.add_argument("--font-h1", default="", help="TTF/OTF path for chapter titles.")
    ap.add_argument("--font-h2", default="", help="TTF/OTF path for section headings.")
    ap.add_argument("--font-body", default="", help="TTF/OTF path for body text.")
    ap.add_argument("--font-body-bold", default="", help="Bold companion for body.")
    ap.add_argument("--font-body-italic", default="", help="Italic companion for body.")
    ap.add_argument("--font-code", default="", help="Monospace TTF/OTF for code.")
    args = ap.parse_args()

    font_spec = {
        k: v
        for k, v in {
            "h1": args.font_h1,
            "h2": args.font_h2,
            "body": args.font_body,
            "body_bold": args.font_body_bold,
            "body_italic": args.font_body_italic,
            "code": args.font_code,
        }.items()
        if v
    } or None

    if args.from_md:
        md_path = Path(args.from_md)
        if not md_path.is_file():
            log(f"Markdown file not found: {md_path}")
            return 2
        pdf_path = md_path.with_name("guide.pdf")
        img_dir = md_path.parent / "images"
        try:
            write_pdf(md_path, pdf_path, img_dir, font_spec)
            log(f"Wrote {pdf_path}")
            return 0
        except Exception as exc:
            log(f"PDF rebuild failed: {exc}")
            return 6

    if not args.url:
        ap.print_help()
        return 1

    custom_ts = None
    if args.timestamps:
        custom_ts = []
        for bit in args.timestamps.split(","):
            val = parse_timestamp_str(bit.strip())
            if val is not None:
                custom_ts.append(val)

    return capture_youtube_video(
        video_input=args.url,
        out_dir_arg=args.out,
        format_opt=args.format,
        shots_per_chapter=args.shots_per_chapter,
        interval_sec=args.interval,
        custom_timestamps=custom_ts,
        resolution=args.resolution,
        preferred_lang=args.lang,
        title_override=args.title,
        font_spec=font_spec,
    )


if __name__ == "__main__":
    sys.exit(main())
