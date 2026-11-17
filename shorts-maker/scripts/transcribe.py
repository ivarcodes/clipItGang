#!/usr/bin/env python3
"""Transcribe a video/audio file with faster-whisper (word-level timestamps).

Writes transcript.json (words + segments) and transcript.txt (readable, in seconds).

Long files are split into audio chunks (default 10 minutes) so memory stays low
and each finished chunk is cached: if the job is interrupted, re-running it
skips completed chunks. `--chunk-seconds 0` disables chunking.

Resource use is automatic: `--threads auto` (default) detects OBS and caps the
job while OBS is running (2 cores, nice 19, ionice idle), then opens up when OBS
is off (up to 6 cores, ionice best-effort). The script only reads /proc to detect
OBS - it NEVER closes, stops, restarts or signals OBS.
"""
import argparse, json, os, subprocess, sys, threading, time
from pathlib import Path

import resources


def probe_duration(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def split_audio(media, chunk_dir, seconds):
    """Extract 16 kHz mono WAV chunks with ffmpeg. Memory stays flat."""
    chunk_dir.mkdir(parents=True, exist_ok=True)
    pattern = str(chunk_dir / "chunk_%04d.wav")
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-i", str(media), "-vn", "-ac", "1", "-ar", "16000",
           "-c:a", "pcm_s16le", "-f", "segment",
           "-segment_time", str(seconds), "-reset_timestamps", "1", pattern]
    subprocess.run(cmd, check=True)
    return sorted(chunk_dir.glob("chunk_*.wav"))


def transcribe_chunk(model, chunk, cache, language, offset, label):
    """Transcribe one chunk, cache it, return it shifted to global time."""
    if cache.exists():
        data = json.loads(cache.read_text())
    else:
        seg_iter, info = model.transcribe(
            str(chunk), language=language, word_timestamps=True,
            vad_filter=True, beam_size=5)
        segments, words = [], []
        for seg in seg_iter:
            text = seg.text.strip()
            if not text:
                continue
            segments.append({"s": round(seg.start, 2), "e": round(seg.end, 2),
                             "text": text})
            for w in seg.words or []:
                token = w.word.strip()
                if token:
                    words.append({"w": token, "s": round(w.start, 3),
                                  "e": round(w.end, 3)})
            print(f"\r{label}  {seg.end:8.1f}s", end="", file=sys.stderr, flush=True)
        data = {"language": info.language, "duration": info.duration,
                "segments": segments, "words": words}
        cache.write_text(json.dumps(data, ensure_ascii=False))

    for seg in data["segments"]:
        seg["s"] = round(seg["s"] + offset, 2)
        seg["e"] = round(seg["e"] + offset, 2)
    for w in data["words"]:
        w["s"] = round(w["s"] + offset, 3)
        w["e"] = round(w["e"] + offset, 3)
    return data


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("media")
    ap.add_argument("--out", default="shorts_work")
    ap.add_argument("--model", default="small", help="tiny/base/small/medium/large-v3")
    ap.add_argument("--language", default=None, help="e.g. en; auto-detect if omitted")
    ap.add_argument("--device", default="auto", help="auto/cpu/cuda")
    ap.add_argument("--threads", default="auto",
                    help="auto (default) detects OBS; or a fixed number of CPU threads")
    ap.add_argument("--chunk-seconds", type=int, default=600,
                    help="audio chunk length in seconds; 0 disables chunking (default 600)")
    ap.add_argument("--start", type=float, default=0.0,
                    help="only transcribe from this time (seconds)")
    ap.add_argument("--end", type=float, default=None,
                    help="only transcribe up to this time (seconds)")
    args = ap.parse_args()

    auto = str(args.threads).lower() == "auto"
    if auto:
        pol = resources.policy()
        resources.apply_self(pol)
        threads = pol["model_threads"]
    else:
        pol = None
        threads = int(args.threads)

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        sys.exit("faster-whisper is not installed. Run: pip install faster-whisper")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    media = Path(args.media)
    slice_offset = 0.0
    if args.start or args.end is not None:
        slice_offset = max(0.0, args.start)
        stop = args.end if args.end is not None else probe_duration(media)
        slice_path = out / "._slice.wav"
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-ss", f"{slice_offset:.3f}", "-i", str(media),
             "-t", f"{max(0.1, stop - slice_offset):.3f}",
             "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
             str(slice_path)], check=True)
        media = slice_path

    if auto:
        print(f"resources: OBS {'running' if pol['obs'] else 'off'} -> "
              f"{threads} model threads on {pol['threads']} core(s), "
              f"nice {pol['nice']}, ionice {pol['ionice']}", file=sys.stderr, flush=True)

    compute = "int8" if args.device in ("auto", "cpu") else "float16"
    model = WhisperModel(args.model, device=args.device, compute_type=compute,
                         cpu_threads=threads, num_workers=1)

    if auto:
        def watch():
            last = (pol["obs"], pol["threads"], pol["nice"], pol["ionice"])
            while True:
                time.sleep(10)
                p = resources.policy()
                key = (p["obs"], p["threads"], p["nice"], p["ionice"])
                if key != last:
                    resources.apply_pid(os.getpid(), p)
                    print(f"resources: OBS {'running' if p['obs'] else 'off'} -> "
                          f"{p['threads']} core(s), nice {p['nice']}, "
                          f"ionice {p['ionice']}", file=sys.stderr, flush=True)
                    last = key

        threading.Thread(target=watch, daemon=True).start()

    duration = probe_duration(media)
    use_chunks = args.chunk_seconds > 0 and duration > args.chunk_seconds

    segments, words, total = [], [], 0.0
    language = args.language

    if use_chunks:
        chunk_dir = out / "._chunks"
        chunks = split_audio(media, chunk_dir, args.chunk_seconds)
        offset = 0.0
        for i, chunk in enumerate(chunks, 1):
            cache = chunk_dir / (chunk.stem + ".json")
            label = f"chunk {i}/{len(chunks)}"
            data = transcribe_chunk(model, chunk, cache, args.language, offset, label)
            segments.extend(data["segments"])
            words.extend(data["words"])
            total += data["duration"]
            language = language or data["language"]
            offset += data["duration"]
            print(f"\r{label} done ({offset:.0f}s / {duration:.0f}s)", file=sys.stderr, flush=True)
        print(file=sys.stderr)
    else:
        seg_iter, info = model.transcribe(
            str(media), language=args.language, word_timestamps=True,
            vad_filter=True, beam_size=5)
        for seg in seg_iter:
            text = seg.text.strip()
            if not text:
                continue
            segments.append({"s": round(seg.start, 2), "e": round(seg.end, 2),
                             "text": text})
            for w in seg.words or []:
                token = w.word.strip()
                if token:
                    words.append({"w": token, "s": round(w.start, 3),
                                  "e": round(w.end, 3)})
            print(f"\r{seg.end:8.1f}s / {info.duration:.0f}s", end="",
                  file=sys.stderr, flush=True)
        print(file=sys.stderr)
        total = info.duration
        language = info.language

    if slice_offset:
        for seg in segments:
            seg["s"] = round(seg["s"] + slice_offset, 2)
            seg["e"] = round(seg["e"] + slice_offset, 2)
        for w in words:
            w["s"] = round(w["s"] + slice_offset, 3)
            w["e"] = round(w["e"] + slice_offset, 3)

    (out / "transcript.json").write_text(json.dumps(
        {"language": language, "duration": total,
         "segments": segments, "words": words}, ensure_ascii=False))
    (out / "transcript.txt").write_text(
        "\n".join(f"[{s['s']:.1f}-{s['e']:.1f}] {s['text']}" for s in segments),
        encoding="utf-8")
    print(f"language={language} duration={total:.0f}s "
          f"segments={len(segments)} words={len(words)}")
    print(out / "transcript.txt")


if __name__ == "__main__":
    main()
