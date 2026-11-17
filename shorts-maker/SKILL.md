---
name: shorts-maker
description: Turn long videos (podcasts, interviews, streams, lectures, tutorials) into vertical 9:16 YouTube Shorts / TikToks / Reels with burned-in captions, driven only by a plain-English prompt. Transcribes with Whisper, lets the LLM pick the best moments, then cuts, reframes and captions with FFmpeg. Use this skill whenever the user wants to clip, cut, trim or repurpose a video into shorts, find highlights or viral moments, make vertical clips, add captions to clips, or says things like "make shorts from this video" or "find the 5 best moments", even if they never mention FFmpeg or Whisper. Only for videos the user owns or has rights to use.
compatibility: Needs python3, ffmpeg + ffprobe on PATH, and pip packages faster-whisper (transcription) and yt-dlp (only if downloading from a URL). Needs a shell with a filesystem.
---

# Shorts Maker

Turn a long video plus a prompt like "find 5 funny moments" into finished, captioned 9:16 shorts. No manual cutting.

The split of work matters: **you (the LLM) decide which moments to clip; the scripts do all the FFmpeg work.** Never write raw FFmpeg commands for the cuts yourself. Write a small `clips.json` and let `render_clips.py` build the commands. That keeps cuts frame-accurate, snapped to word boundaries, and consistent.

Clip selection is plain transcript reading by whatever LLM/agent runs this skill: no API keys, no vendor SDKs, so it works with any model or provider.

## Rights check

Only process videos the user owns, made, or has permission/licence to reuse (their own channel, client work, Creative Commons, etc.). If the user clearly wants to re-upload someone else's video, tell them it risks copyright strikes and ToS violations, and offer to work on their own content instead. Don't lecture beyond that.

## Workflow

Work in a project folder, e.g. `./shorts_work/`. Scripts live next to this file in `scripts/`.

### 0. Check setup (once)

```bash
ffmpeg -version | head -1
python3 -c "import faster_whisper" || pip install faster-whisper
# only for URLs:
python3 -c "import yt_dlp" || pip install yt-dlp
```

If ffmpeg is missing, tell the user how to install it (`brew install ffmpeg`, `sudo apt install ffmpeg`, or `winget install ffmpeg`) rather than trying to work around it.

### 1. Get the video

- Local file: use it directly.
- URL (their own content): `python3 scripts/ingest.py "<url>" --out shorts_work/`  → prints the path of the downloaded `source.mp4`.

### 2. Transcribe

```bash
python3 scripts/transcribe.py shorts_work/source.mp4 --out shorts_work/ --model small
```

Produces:
- `transcript.json`: word-level timestamps, used by the renderer for snapping and captions
- `transcript.txt`: readable `[start-end] sentence` lines in seconds, which is what you read

Model guidance: `base` for quick tests, `small` default, `medium`/`large-v3` for accuracy if the machine has a GPU. Add `--language en` (or other code) to skip auto-detection. Tell the user it can take a while on long videos.

Keep the machine usable: `--threads auto` is the default and auto-detects OBS. With OBS running, the script caps itself to 2 cores, nice 19 and idle I/O; with OBS off it opens up to 6 cores. It only reads `/proc` to detect OBS and never closes, stops or signals it. Pass `--threads N` only to override.

### 3. Choose the clips (your job)

Read `transcript.txt` (in chunks if it is long) and pick moments that match the user's prompt. If the prompt is vague, default to the most engaging, self-contained moments. Good Shorts clips:

- **20-59 seconds** (hard max 59 so it qualifies as a Short; 25-45 is the sweet spot)
- **Open on a hook**: a bold claim, a question, a surprising statement, or the start of a story. Skip clips that begin with "so, um, anyway" or depend on earlier context ("like I said before").
- **Stand alone**: a viewer with zero context should understand it.
- **Have a payoff**: end right after the punchline, answer or conclusion, not mid-thought.
- **Start and end on sentence boundaries.**
- Don't overlap clips.

Write `shorts_work/clips.json`:

```json
{
  "source": "shorts_work/source.mp4",
  "clips": [
    {
      "start": 312.4,
      "end": 351.9,
      "title": "Why most startups die in year two",
      "hook": "Most startups don't fail because of competition.",
      "reason": "Strong claim up front, clear story, ends on the takeaway"
    }
  ]
}
```

Times are in seconds from the transcript. The renderer snaps them to the nearest word boundaries, so you don't need to be perfect to the millisecond. `title` becomes the filename and is useful for the upload. `hook` and `reason` are for the user's review and aren't rendered. Add `"index": 2` to control the filename number when adding clips to an existing set.

Optional per-clip fields: `"reframe": "blur" | "crop"` and `"crop_x": 0.0-1.0` (horizontal focus for crop mode, 0.5 = centre, use it when the speaker is off-centre).

To cut dead air or mash several moments into one clip, use `segments` instead of `start`/`end`. Each segment can have its own `crop_x`:

```json
{
  "index": 7,
  "title": "More Than My Paycheck",
  "crop_x": 0.0,
  "segments": [
    {"start": 4441.0, "end": 4471.2, "crop_x": 0.0},
    {"start": 4473.8, "end": 4497.5, "crop_x": 0.0}
  ]
}
```

The renderer trims and concatenates the segments in one FFmpeg pass, shifts captions onto the combined timeline, and enforces the 59s cap across all segments.

Show the user a short list of the chosen clips (title, timestamps, one-line reason) before rendering when there are many, or if they asked to review. For a simple "just make them" request, go straight on to rendering.

### 4. Render

```bash
python3 scripts/render_clips.py shorts_work/clips.json --transcript shorts_work/transcript.json --out shorts_work/shorts/
```

Options:
- `--reframe crop` (default): fills the whole 9:16 frame by cropping the sides, no blur bars. Use `crop_x` per clip to aim at the speaker (0.5 = centre).
- `--reframe blur`: full video centred on a blurred, zoomed copy of itself. Safe for any footage, never cuts off people, but shows bars.
- `--no-captions` to skip burned-in captions
- `--caption-style bold|clean` (default `bold`: heavy Anton font, white words with the spoken word in yellow)
- `--max-words 3` words per caption line
- `--max-duration 59` hard cap in seconds (default; keeps every clip under 1 minute)
- `--threads auto` detects OBS per clip (2 cores while streaming, up to 6 when off; never touches OBS)
- `--crf 20` quality (lower = better/larger), `--preset medium`
- `--only N` render just queue item N (1-based), handy for resuming a queue one clip at a time
- `--clean` deletes temp transcription chunks and logs after the render succeeds
- final cleanup: `scripts/cleanup.py --final` removes `transcriptions/`, `samples/`,
  `clips.json` and logs, leaving only `clips/` and `reference/` (plus the tooling,
  `.venv` and `prompt.txt`). Add `--dry-run` to preview. It refuses to run while a
  render or transcription is active.

While rendering, the script prints a live terminal progress bar with elapsed time, percentage and ETA for each clip.

Outputs `NN_title.mp4` files (1080x1920, H.264, AAC, loudness-normalised) plus `NN_title.ass` captions.

### 5. Report back

List the files with durations, and mention that each clip is editable: the user can say "make clip 2 start earlier", "drop clip 3", "use crop mode" and you re-run only the render with an updated `clips.json`. Offer a title/description/hashtags for each clip if they want to upload.

## Common problems

- **Captions have wrong words**: re-transcribe with a bigger model, or pass `--language`.
- **Speaker cut off in crop mode**: switch to `blur`, or set `crop_x`.
- **`libass`/`subtitles` filter missing**: the ffmpeg build lacks libass; use a full build (e.g. from ffmpeg.org builds or `brew install ffmpeg`) or run with `--no-captions`.
- **Huge file, slow transcription**: use `--model base` first, then redo with a larger model only for the final pass.
- **Video has no speech** (gameplay, music): transcription won't help pick moments. Ask the user for timestamps or add audio-energy detection (see `references/ideas.md`).
