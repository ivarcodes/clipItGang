#!/usr/bin/env python3
"""Render vertical 9:16 shorts from clips.json using FFmpeg.

The LLM writes clips.json (start/end/title). This script snaps the cuts to word
boundaries, builds captions, reframes to 1080x1920 and exports.

Resource use is automatic: `--threads auto` (default) detects OBS per clip and
uses 2 cores while OBS is running, up to 6 cores when it is off. It only reads
/proc to detect OBS - it NEVER closes, stops, restarts or signals OBS.
"""
import argparse, json, re, subprocess, sys, shutil, time
from pathlib import Path

import resources

W, H = 1080, 1920
ROOT = Path(__file__).resolve().parent.parent
FONTS_DIR = ROOT / "assets" / "fonts"


def slug(text, maxlen=50):
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return (s or "clip")[:maxlen].strip("-")


def probe_duration(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None


def snap(start, end, words, total):
    """Move start/end to word boundaries so no word is cut in half."""
    if words:
        # first word that begins at or after (start - 0.35)
        cand = [w for w in words if w["s"] >= start - 0.35]
        if cand:
            start = cand[0]["s"]
        # last word that ends at or before (end + 0.35)
        cand = [w for w in words if w["e"] <= end + 0.35]
        if cand:
            end = cand[-1]["e"]
    start = max(0.0, start - 0.15)   # tiny lead-in
    end = end + 0.25                 # tiny tail so the last word isn't clipped
    if total:
        end = min(end, total)
    return start, end


def clamp_duration(start, end, words, max_dur):
    """Keep the clip under max_dur, cutting at the last word that fits."""
    limit = start + max_dur
    if end <= limit:
        return end
    cand = [w["e"] for w in words if w["s"] >= start and w["e"] <= limit]
    return max(cand) if cand else limit


def ass_time(t):
    t = max(t, 0)
    h = int(t // 3600); m = int((t % 3600) // 60); s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def plan_segments(clip, words, total, max_dur):
    """Snap each requested segment to words and cap the total clip length."""
    raw = clip.get("segments") or [{"start": clip.get("start"), "end": clip.get("end")}]
    default_crop = float(clip.get("crop_x", 0.5))
    plans = []
    for seg in raw:
        s, e = snap(float(seg["start"]), float(seg["end"]), words, total)
        crop = min(1.0, max(0.0, float(seg.get("crop_x", default_crop))))
        if e - s >= 0.8:
            plans.append([s, e, crop])
    remaining, final = max_dur, []
    for s, e, crop in plans:
        if remaining <= 0.5:
            break
        if e - s > remaining:
            e = clamp_duration(s, s + remaining, words, remaining)
        if e - s >= 0.8:
            final.append((s, e, crop))
            remaining -= e - s
    return final


def timeline_words(words, plans):
    """Shift each segment's words onto the concatenated clip timeline."""
    out, offset = [], 0.0
    for s, e, _crop in plans:
        for w in words:
            if w["s"] >= s - 0.05 and w["e"] <= e + 0.05:
                out.append({"w": w["w"],
                            "s": round(w["s"] - s + offset, 3),
                            "e": round(w["e"] - s + offset, 3)})
        offset += e - s
    return out


def build_ass(clip_words, style, max_words, path):
    """Build captions. Bold style = heavy font with the spoken word in yellow."""
    if style == "bold":
        font = "Anton" if (FONTS_DIR / "Anton-Regular.ttf").exists() else "DejaVu Sans"
        size, outline, shadow, margin_v = 104, 7, 3, 470
        bold_flag = 0  # Anton is already heavy; avoid synthetic bold blur
        white, active = "&H00FFFFFF", "&H0000E6FF"  # white / yellow (BGR)
    else:
        font = "Liberation Sans"
        size, outline, shadow, margin_v = 64, 4, 2, 520
        bold_flag = -1
        white = active = "&H00FFFFFF"
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{size},{white},{white},&H00000000,&H78000000,{bold_flag},0,0,0,100,100,0,0,1,{outline},{shadow},2,60,60,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    for i in range(0, len(clip_words), max_words):
        chunk = clip_words[i:i + max_words]
        c_start = chunk[0]["s"]
        c_end = chunk[-1]["e"] + 0.2
        if i + max_words < len(clip_words):
            c_end = min(clip_words[i + max_words]["s"], c_end)
        if style != "bold":
            parts = []
            for j, w in enumerate(chunk):
                nxt = chunk[j + 1]["s"] if j + 1 < len(chunk) else w["e"]
                cs = max(1, round((nxt - w["s"]) * 100))
                parts.append(f"{{\\kf{cs}}}{w['w'].replace('{', '(').replace('}', ')')}")
            lines.append(f"Dialogue: 0,{ass_time(c_start)},{ass_time(c_end)},"
                         f"Default,,0,0,0,,{' '.join(parts)}")
            continue
        # one event per word, whole active word turns yellow
        for j, w in enumerate(chunk):
            ev_start = w["s"]
            ev_end = chunk[j + 1]["s"] if j + 1 < len(chunk) else c_end
            if ev_end <= ev_start:
                continue
            parts = []
            for k, token in enumerate(chunk):
                text = token["w"].replace("{", "(").replace("}", ")").upper()
                color = active if k == j else white
                parts.append(f"{{\\c{color}}}{text}")
            lines.append(f"Dialogue: 0,{ass_time(ev_start)},{ass_time(ev_end)},"
                         f"Default,,0,0,0,,{' '.join(parts)}")
    Path(path).write_text(header + "\n".join(lines) + "\n", encoding="utf-8")
    return len(lines)


def build_filter(mode, crops, ass_name, loudnorm):
    """Build one graph that crops every segment, concatenates them, then captions."""
    parts = []
    for i, crop_x in enumerate(crops):
        if mode == "crop":
            # full-frame 9:16: scale to cover, crop a 1080-wide window, sharpen a touch
            parts.append(
                f"[{i}:v]scale=-2:{H}:flags=lanczos,"
                f"crop={W}:{H}:x='max(0,min(iw-{W},(iw-{W})*{crop_x}))':y=0,"
                f"unsharp=5:5:0.35:3:3:0.0,setsar=1,fps=30,format=yuv420p,"
                f"setpts=PTS-STARTPTS[v{i}]")
        else:
            parts.append(
                f"[{i}:v]split=2[a{i}][b{i}];"
                f"[a{i}]scale={W}:{H}:force_original_aspect_ratio=increase,"
                f"crop={W}:{H},boxblur=40:5,eq=brightness=-0.05[bg{i}];"
                f"[b{i}]scale={W}:-2:flags=lanczos[fg{i}];"
                f"[bg{i}][fg{i}]overlay=(W-w)/2:(H-h)/2,setsar=1,fps=30,"
                f"format=yuv420p,setpts=PTS-STARTPTS[v{i}]")
        parts.append(
            f"[{i}:a]aresample=48000,"
            f"aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"asetpts=PTS-STARTPTS[a{i}]")
    pairs = "".join(f"[v{i}][a{i}]" for i in range(len(crops)))
    graph = ";".join(parts) + ";" + f"{pairs}concat=n={len(crops)}:v=1:a=1[vc][ac];"
    if ass_name:
        fonts = FONTS_DIR if (FONTS_DIR / "Anton-Regular.ttf").exists() else None
        opts = f"subtitles={ass_name}"
        if fonts:
            opts += f":fontsdir={fonts}"
        graph += f"[vc]{opts}[v];"
    else:
        graph += "[vc]null[v];"
    if loudnorm:
        graph += "[ac]loudnorm=I=-14:TP=-1.5:LRA=11[a]"
    else:
        graph += "[ac]anull[a]"
    return graph


def fmt_time(sec):
    sec = max(0, int(sec))
    return f"{sec // 60:02d}:{sec % 60:02d}"


def run_ffmpeg(cmd, out_dir, duration, label):
    """Run FFmpeg and draw a live terminal progress bar with ETA."""
    proc = subprocess.Popen(
        cmd, cwd=out_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1)
    started = time.time()
    tty = sys.stdout.isatty()
    out_time, last_pct, last_draw, width = 0.0, -1, 0.0, 26
    while True:
        line = proc.stdout.readline()
        if not line:
            break
        line = line.strip()
        if line.startswith("out_time="):
            raw = line.split("=", 1)[1]
            try:
                h, m, s = raw.split(":")
                out_time = int(h) * 3600 + int(m) * 60 + float(s)
            except ValueError:
                continue
        elif line.startswith("progress=") and line.endswith("end"):
            out_time = duration
        else:
            continue
        now = time.time()
        frac = min(1.0, out_time / duration) if duration else 0.0
        pct = int(frac * 100)
        if pct == last_pct or (frac < 1 and now - last_draw < 0.25):
            continue
        last_pct, last_draw = pct, now
        elapsed = now - started
        eta = elapsed * (1 - frac) / frac if frac > 0.01 else None
        eta_text = fmt_time(eta) if eta is not None else "--:--"
        if tty:
            filled = int(frac * width)
            bar = "█" * filled + "░" * (width - filled)
            print(f"\r  {label} [{bar}] {frac * 100:5.1f}%  "
                  f"{fmt_time(out_time)}/{fmt_time(duration)}  ETA {eta_text}   ",
                  end="", flush=True)
        else:
            print(f"  {label} {pct:3d}%  {fmt_time(out_time)}/{fmt_time(duration)}  "
                  f"ETA {eta_text}", flush=True)
    err = proc.stderr.read()
    code = proc.wait()
    if tty:
        print("\r" + " " * 100 + "\r", end="", flush=True)
    return code, err, time.time() - started


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("clips", help="clips.json")
    ap.add_argument("--transcript", default=None, help="transcript.json (for snapping + captions)")
    ap.add_argument("--source", default=None, help="override source video path")
    ap.add_argument("--out", default="shorts_work/shorts")
    ap.add_argument("--reframe", choices=["blur", "crop"], default="crop")
    ap.add_argument("--no-captions", action="store_true")
    ap.add_argument("--caption-style", choices=["bold", "clean"], default="bold")
    ap.add_argument("--max-words", type=int, default=3)
    ap.add_argument("--max-duration", type=float, default=59.0,
                    help="hard cap in seconds, keeps clips under 1 minute (default 59)")
    ap.add_argument("--threads", default="auto",
                    help="auto (default) detects OBS; or a fixed number of FFmpeg threads")
    ap.add_argument("--crf", type=int, default=20)
    ap.add_argument("--preset", default="medium")
    ap.add_argument("--no-loudnorm", action="store_true")
    ap.add_argument("--clean", action="store_true",
                    help="remove temp chunks/logs after all clips render successfully")
    ap.add_argument("--only", type=int, default=None,
                    help="render only queue item N (1-based), useful to resume the queue")
    args = ap.parse_args()

    auto = str(args.threads).lower() == "auto"
    if auto:
        resources.apply_self(resources.policy())

    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg not found on PATH.")

    spec = json.loads(Path(args.clips).read_text())
    if args.only is not None and not (1 <= args.only <= len(spec["clips"])):
        sys.exit(f"--only must be between 1 and {len(spec['clips'])}")
    source = Path(args.source or spec.get("source", ""))
    if not source.is_file():
        sys.exit(f"Source video not found: {source}")
    words = []
    if args.transcript and Path(args.transcript).exists():
        words = json.loads(Path(args.transcript).read_text()).get("words", [])
    captions = (not args.no_captions) and bool(words)
    if not args.no_captions and not words:
        print("warning: no transcript words, rendering without captions", file=sys.stderr)

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    total = probe_duration(source)
    source = source.resolve()
    manifest_path = out / "manifest.json"
    results = []
    if args.only is not None and manifest_path.exists():
        try:
            results = json.loads(manifest_path.read_text())
        except Exception:
            results = []
    rendered = []

    for i, clip in enumerate(spec["clips"], 1):
        if args.only is not None and i != args.only:
            continue
        plans = plan_segments(clip, words, total, args.max_duration)
        dur = sum(e - s for s, e, _crop in plans)
        if dur < 3:
            print(f"skip clip {i}: too short ({dur:.1f}s)", file=sys.stderr)
            continue
        idx = int(clip.get("index", i))
        name = f"{idx:02d}_{slug(clip.get('title', 'clip'))}"
        mp4 = out / f"{name}.mp4"
        ass_name = None
        if captions:
            ass_name = f"{name}.ass"
            tw = timeline_words(words, plans)
            n = build_ass(tw, args.caption_style, args.max_words, out / ass_name)
            if n == 0:
                ass_name = None
        mode = clip.get("reframe", args.reframe)
        crops = [crop for _s, _e, crop in plans]
        fc = build_filter(mode, crops, ass_name, not args.no_loudnorm)

        if auto:
            pol = resources.policy()
            threads = pol["threads"]
            prefix = ["taskset", "-c", pol["cpu_list"],
                      "ionice", "-c", str(pol["ionice"])]
            res_note = f"OBS {'on' if pol['obs'] else 'off'}, {threads} cores"
        else:
            threads = int(args.threads)
            prefix = []
            res_note = f"{threads} threads"

        cmd = prefix + ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
               "-nostats", "-progress", "pipe:1"]
        for s, e, _crop in plans:
            cmd += ["-ss", f"{s:.3f}", "-t", f"{e - s:.3f}", "-i", str(source)]
        cmd += ["-filter_complex", fc, "-threads", str(threads),
                "-map", "[v]", "-map", "[a]",
                "-c:v", "libx264", "-crf", str(args.crf), "-preset", args.preset,
                "-pix_fmt", "yuv420p", "-r", "30", "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
                "-movflags", "+faststart", mp4.name]
        # run inside out/ so the subtitles filter can use a plain relative filename
        seg_note = " + ".join(f"{s:.1f}-{e:.1f}" for s, e, _crop in plans)
        print(f"[{i}/{len(spec['clips'])}] {name}  {seg_note}s ({dur:.1f}s, {mode}, {res_note})")
        code, err, took = run_ffmpeg(cmd, out, dur, name)
        if code != 0:
            print(err[-1500:], file=sys.stderr)
            if "subtitles" in err.lower() or "libass" in err.lower():
                print("Your ffmpeg may lack libass. Use a full ffmpeg build or --no-captions.", file=sys.stderr)
            continue
        print(f"  ✓ {name} rendered in {fmt_time(took)}")
        record = {"index": idx, "queue": i, "file": str(mp4),
                  "segments": [{"start": round(s, 2), "end": round(e, 2),
                                "crop_x": round(c, 2)} for s, e, c in plans],
                  "start": round(plans[0][0], 2), "end": round(plans[-1][1], 2),
                  "duration": round(dur, 1), "title": clip.get("title", "")}
        results = [r for r in results if r.get("index") != idx] + [record]
        results.sort(key=lambda r: r.get("index", 0))
        manifest_path.write_text(json.dumps(results, indent=2))
        rendered.append(record)

    print(f"\nDone: {len(rendered)} short(s) in {out}")
    for r in rendered:
        print(f"  {r['duration']:>5}s  {r['file']}")

    if args.clean and results and len(results) == len(spec["clips"]):
        subprocess.run([sys.executable, str(Path(__file__).with_name("cleanup.py"))],
                       check=False)


if __name__ == "__main__":
    main()
