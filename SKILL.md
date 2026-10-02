---
name: youtube-capture
description: Capture a YouTube video tutorial, walkthrough, presentation, or devlog as a high-quality local How-To Guide (markdown + selective interface images + publication-grade PDF). Synthesizes video transcripts and visual steps into actionable how-to instructions with clear UI navigation paths and targeted interface figures. Use when the user says "turn this video into a guide", "make a tutorial from this youtube video", "capture youtube video", "youtube how-to guide", "youtube to pdf", "turn this video into a PDF", or runs /youtube-capture.
---

# YouTube Capture: How-To Guide Generator

Transform YouTube video tutorials, walkthroughs, presentations, and technical devlogs into actionable, publication-grade local How-To Guides: `guide.md`, `images/`, optional `guide.pdf`, and `manifest.json`.

Rather than taking unnecessary or repetitive screenshots, this skill synthesizes the video transcript and demonstration into a structured technical tutorial. When buttons need to be clicked or settings configured, it provides explicit navigation paths (e.g. `Navigate to: Menu > Settings > API Key` or `Click **[Button Name]** at the bottom-right corner`) and includes purposeful interface figures only where visual orientation or complex dialogs are helpful.

Every figure includes a **visible** `**Figure note.**` under it with a complete visual description (on-screen controls, highlighted buttons, active values, and UI state).

The generated PDF is formatted as a **clean technical book** using ReportLab (Inter / Segoe UI typography, Geist Mono / Consolas code blocks, Pygments syntax highlighting, and two-column contents).

## Guide Structure

Every how-to guide follows this structured order (in both `guide.md` and `guide.pdf`):

1. **Cover & Metadata** — Video title, creator channel, duration, and capture date.
2. **Overview & Objectives** — Concise synthesis of what the tutorial accomplishes, what problem it solves, and the end result.
3. **Prerequisites & Requirements** — Required software, DAW/OS versions, API keys, dependencies, or sample files needed before starting.
4. **Contents** — Table of contents with chapter titles and timestamp links (`_Video timestamp: [MM:SS](URL&t=...s)_`).
5. **Step-by-Step Instructions** — Actionable, sequential steps:
   - **Explicit Navigation Paths**: Document exact menu hierarchies and click locations (e.g., `Click **Settings** on the top-right device header > Navigate to **Stability Audio API Key**`).
   - **Selective Visual Figures**: Include targeted screenshots only for key UI layouts, settings dialogs, parameter dials, or visual outputs (waveforms, graphs, renders). Do NOT capture redundant or static frames.
   - **Parameter Tables & Code**: Use tables for knob/slider settings and fenced code blocks for prompts, terminal commands, or code snippets.
6. **Troubleshooting & Pro Tips** — Practical advice, common pitfalls, edge cases, and performance recommendations mentioned or implied in the video.
7. **Sources & Attribution** — Link to the original YouTube video, creator channel, and transcript/caption metadata.

## Run

```bash
python "<skill-dir>/scripts/capture_youtube.py" "<YOUTUBE_URL_OR_ID>" --out "<OUT_DIR>" --format both
```

`<skill-dir>` is this skill's directory (e.g. `~/.claude/skills/youtube-capture`, `~/.agents/skills/youtube-capture`, `~/.gemini/antigravity/skills/youtube-capture`, `~/.gemini/config/skills/youtube-capture`, or `~/.grok/skills/youtube-capture`).

| Flag | Default | Meaning |
| :--- | :--- | :--- |
| `--format` | `both` | `md`, `pdf`, or `both` |
| `--shots-per-chapter` | `2` | Max still frames per chapter (only keeps distinct UI states) |
| `--interval` | `45` | Fallback interval in seconds for sampling |
| `--timestamps` | none | Comma-separated list of explicit key UI timestamps (e.g. `0:20,1:30,3:00`) |
| `--resolution` | `720` | Video resolution height for interface capture (default: 720) |
| `--lang` | `en` | Subtitle/transcript language code |
| `--title` | auto | Custom guide title override |
| `--from-md PATH` | off | Rebuild `guide.pdf` from an edited `guide.md` (no video download) |
| `--font-h1 PATH` | auto | TTF/OTF path for chapter titles |
| `--font-h2 PATH` | auto | TTF/OTF path for section headings |
| `--font-body PATH` | auto | TTF/OTF path for body text |
| `--font-body-bold PATH` | auto | Bold companion for body |
| `--font-body-italic PATH` | auto | Italic companion for body |
| `--font-code PATH` | auto | Monospace font for code blocks |

## Workflow Steps

1. **Resolve Video URL**: Accept `https://youtube.com/watch?v=...`, `https://youtu.be/...`, `https://youtube.com/shorts/...`, or video ID.
2. **Output Directory**: Default to `~/Documents/guides/<video-slug>/` unless an alternative path is specified.
3. **Execute Extraction**:
   - Pulls video metadata, chapter markers, and transcripts (via `yt-dlp` and caption fallbacks).
   - Extracts key still frames using `ffmpeg` at significant UI transition points.
4. **Selective Visual Curation**:
   - Inspect extracted images in `images/`. Remove unnecessary, duplicate, or uninformative screenshots.
   - Keep only images that provide necessary visual orientation (e.g., complex menu locations, parameter dials, multi-tab settings dialogs, waveform/result comparisons).
5. **Write Visible Figure Notes**:
   - For every retained image in `images/`, provide a detailed `**Figure note.**`:
     - Exactly what UI panel, dialog, or demonstration is depicted.
     - Highlighted buttons, active sliders, entered values, and visual states.
     - Spell out all icons in plain words (`settings gear`, `play icon`, `three-dot menu`, `check mark`, `right arrow`). Never use raw emoji or dingbats.
6. **Author Actionable How-To Guide (`guide.md`)**:
   - Write clear, technical how-to prose from the transcript and video demonstration.
   - Explain exact navigation paths for every click and action.
   - Format UI controls in **bold** (`**Settings**`, `**Generate**`, `**Model Strength**`), parameters in tables, and prompts/commands in code blocks.
7. **Compile Publication PDF**:
   ```bash
   python "<skill-dir>/scripts/capture_youtube.py" --from-md "<OUT_DIR>/guide.md"
   ```
   Recompiles `guide.pdf` with the updated prose, navigation paths, and curated figures.
8. **Deliver Deliverables**: Report the output path, title, chapter breakdown, curated frame count, and clickable links to `guide.md` and `guide.pdf`.
