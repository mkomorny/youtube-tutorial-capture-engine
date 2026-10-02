# YouTube Tutorial Capture Engine

An automated multimedia documentation engine that turns technical YouTube videos, walkthroughs, and developer tutorials into publication-grade, step-by-step How-To Guides (Markdown, extracted keyframe screenshots, and publication-quality PDFs).

## Pipeline Capabilities

1. **Transcript & Timestamp Extraction**: Retrieves native or auto-generated video transcripts with precise millisecond timestamps via `yt-dlp` and `youtube-transcript-api`.
2. **Keyframe Extraction**: Automatically calculates optimal transition timestamps and captures high-resolution screenshots of UI steps, code editors, and workflow milestones using `ffmpeg`.
3. **Structured Markdown Generation**: Synthesizes the video transcript into numbered procedures, bulleted prerequisite checklists, interface element navigation paths, and parameter callouts.
4. **Publication PDF Compilation**: Renders styled PDF guides with syntax-highlighted code panels, figure captions, and clean typography.

## Dependencies

- **Python**: 3.9+
- `yt-dlp`
- `youtube-transcript-api`
- `requests`
- `ffmpeg` (installed and in system PATH)
- `weasyprint` (optional, for PDF rendering)

## Instructions

See [INSTRUCTIONS.md](./INSTRUCTIONS.md) for CLI commands and generation flags.

## License

This project is licensed under the GNU General Public License v3.0 (GPL-3.0) - see the [LICENSE](./LICENSE) file for details.
