# YouTube Tutorial Capture Engine - Usage Guide

## Prerequisites

```bash
pip install -r requirements.txt
```
Ensure `ffmpeg` is installed on your system.

---

## 1. Capturing a Tutorial

Run the capture script on any YouTube video URL:
```bash
python scripts/capture_youtube.py --url "https://www.youtube.com/watch?v=VIDEO_ID" --output ./output/tutorial_guide
```

## 2. Command Options

- `--url`: YouTube video URL or ID.
- `--output`: Destination directory for markdown and extracted images.
- `--pdf`: Automatically compile output into a styled PDF guide.
- `--interval`: Minimum seconds between captured keyframe screenshots (default: 15s).
