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


def plan_segments(clip, words, total, max_dur, default_transition="fade"):
    """Snap each requested segment to words and cap the total clip length."""
    raw = clip.get("segments") or [{"start": clip.get("start"), "end": clip.get("end")}]
    default_crop = float(clip.get("crop_x", 0.5))
    default_transition = clip.get("transition", default_transition)
    plans = []
    for seg in raw:
        if seg.get("snap") is False:
            # pre-cut silence-stripped ranges: keep them exact, no lead/tail
            s, e = float(seg["start"]), float(seg["end"])
        else:
            s, e = snap(float(seg["start"]), float(seg["end"]), words, total)
        crop = min(1.0, max(0.0, float(seg.get("crop_x", default_crop))))
        transition = str(seg.get("transition", default_transition))
        if e - s >= 0.25:
            plans.append([s, e, crop, transition])
    remaining, final = max_dur, []
    for s, e, crop, transition in plans:
        if remaining <= 0.25:
            break
        if e - s > remaining:
            e = clamp_duration(s, s + remaining, words, remaining)
        if e - s >= 0.25:
            final.append((s, e, crop, transition))
            remaining -= e - s
    return final


def timeline_words(words, plans, overlap=0.0):
    """Shift each segment's words onto the finished timeline."""
    out, offset, prev_src = [], 0.0, None
    for i, (s, e, _crop, _transition) in enumerate(plans):
        if i and overlap:
            offset -= overlap
        for w in words:
            # include every word that starts inside the segment; clamp the last
            # word's end to the cut instead of dropping it when it overshoots
            if w["s"] >= s - 0.05 and w["s"] < e - 0.02:
                ws = max(w["s"], s)
                we = min(w["e"], e)
                if we <= ws:
                    continue
                # a jump in source time means a beat/silence cut: restart the
                # caption card there instead of joining phrases across it
                gap = prev_src is not None and abs(w["s"] - prev_src) > 0.55
                out.append({"w": w["w"],
                            "s": round(ws - s + offset, 3),
                            "e": round(we - s + offset, 3),
                            "break": gap})
                prev_src = w["s"]
        offset += e - s
    return out


def jl_windows(plans, lead=0.22):
    """Work out video/audio windows for J-cut and L-cut boundaries.

    Audio is a straight concat of each segment [s, e], so speech is continuous.
    For a J-cut the picture changes `lead` seconds AFTER the sound, for an
    L-cut `lead` seconds BEFORE it. The shifts cancel, so total length is the
    same and caption timing stays on the audio timeline.
    """
    wins = []
    for i, (s, e, crop, transition) in enumerate(plans):
        prev_t = plans[i - 1][3] if i else "cut"
        in_shift = lead if prev_t == "jcut" else (-lead if prev_t == "lcut" else 0.0)
        out_shift = lead if transition == "jcut" else (-lead if transition == "lcut" else 0.0)
        vs, ve = s + in_shift, e + out_shift
        if ve - vs < 0.12:
            vs, ve = s, e
        read_s = min(s, vs)
        read_e = max(e, ve) + 0.02
        wins.append({"s": s, "e": e, "vs": vs, "ve": ve,
                     "read_s": read_s, "read_e": read_e,
                     "crop": crop, "transition": transition})
    return wins


def single_segment_filters(mode, w, fps=30):
    """Video/audio filters for one J/L segment (their windows differ)."""
    a0, a1 = w["s"] - w["read_s"], w["e"] - w["read_s"]
    v0, v1 = w["vs"] - w["read_s"], w["ve"] - w["read_s"]
    crop_x = w["crop"]
    audio = (f"[0:a]asetpts=PTS-STARTPTS,atrim=start={a0:.3f}:end={a1:.3f},"
             f"asetpts=PTS-STARTPTS,aresample=48000,"
             f"aformat=sample_fmts=s16:channel_layouts=stereo[a]")
    pre = (f"[0:v]setpts=PTS-STARTPTS,trim=start={v0:.3f}:end={v1:.3f},"
           f"setpts=PTS-STARTPTS")
    if mode == "crop":
        video = (pre + f",scale=-2:{H}:flags=lanczos,"
                 f"crop={W}:{H}:x='max(0,min(iw-{W},(iw-{W})*{crop_x}))':y=0,"
                 f"unsharp=5:5:0.35:3:3:0.0,setsar=1,fps={fps},format=yuv420p[v]")
    else:
        video = (pre + f",split=2[av][bv];"
                 f"[av]scale={W}:{H}:force_original_aspect_ratio=increase,"
                 f"crop={W}:{H},boxblur=40:5,eq=brightness=-0.05[bg];"
                 f"[bv]scale={W}:-2:flags=lanczos[fg];"
                 f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,fps={fps},"
                 f"format=yuv420p[v]")
    return video, audio


def render_lowmem(wins, mode, source, out, name, mp4, ass_name, prefix,
                  threads, preset, crf, duration, loudnorm=True, fps=30):
    """Render each segment in its own short-lived process, then join once.

    A 50-input filtergraph exhausted RAM and swap. Here each segment is encoded
    in a short-lived process, and video/audio are joined as separate streams so
    the J/L offsets stay exact. PCM intermediates keep the audio gapless;
    captions and loudnorm are applied in one final pass.
    """
    import tempfile
    with tempfile.TemporaryDirectory(prefix="clipper_seg_") as tmp:
        tmp = Path(tmp)
        vids, auds = [], []
        seg_crf = max(12, crf - 2)
        for i, w in enumerate(wins):
            vfc, afc = single_segment_filters(mode, w, fps)
            seek = ["-ss", f"{w['read_s']:.3f}",
                    "-t", f"{w['read_e'] - w['read_s']:.3f}"]
            common = prefix + ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
            vseg, aseg = tmp / f"v{i:04d}.mkv", tmp / f"a{i:04d}.wav"
            for fc, extra, dest in (
                    (vfc, ["-threads", str(threads), "-c:v", "libx264",
                           "-preset", preset, "-crf", str(seg_crf),
                           "-pix_fmt", "yuv420p", "-fps_mode", "cfr",
                           "-r", str(fps), "-an"], vseg),
                    (afc, ["-c:a", "pcm_s16le", "-ar", "48000", "-vn"], aseg)):
                cmd = common + seek + ["-i", str(source), "-filter_complex", fc,
                                       "-map", "[v]" if dest == vseg else "[a]"] + extra + [str(dest)]
                r = subprocess.run(cmd, capture_output=True, text=True)
                if r.returncode != 0:
                    print(r.stderr[-1200:], file=sys.stderr)
                    return r.returncode
            vids.append(vseg)
            auds.append(aseg)
            print(f"  segment {i + 1}/{len(wins)}", flush=True)
        joined_v, joined_a = tmp / "video.mkv", tmp / "audio.wav"
        for items, dest in ((vids, joined_v), (auds, joined_a)):
            listing = tmp / f"list_{dest.stem}.txt"
            listing.write_text("".join(f"file '{p}'\n" for p in items))
            cmd = prefix + ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                            "-f", "concat", "-safe", "0", "-i", str(listing),
                            "-c", "copy", str(dest)]
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                print(r.stderr[-1200:], file=sys.stderr)
                return r.returncode
        fonts = FONTS_DIR if any((FONTS_DIR / n).exists() for n in
                                 ("ArchivoBlack-Regular.ttf", "Anton-Regular.ttf")) else None
        vf = f"subtitles={ass_name}" if ass_name else "null"
        if ass_name and fonts:
            vf += f":fontsdir={fonts}"
        af = "loudnorm=I=-14:TP=-1.5:LRA=11" if loudnorm else "anull"
        cmd = prefix + ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                        "-nostats", "-progress", "pipe:1",
                        "-i", str(joined_v), "-i", str(joined_a),
                        "-vf", vf, "-af", af, "-threads", str(threads),
                        "-map", "0:v:0", "-map", "1:a:0",
                        "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
                        "-pix_fmt", "yuv420p", "-fps_mode", "cfr", "-r", str(fps),
                        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
                        "-movflags", "+faststart", mp4.name]
        code, err, took = run_ffmpeg(cmd, out, duration, name)
        if code != 0:
            print(err[-1200:], file=sys.stderr)
        return code


def build_jl_filter(mode, wins, ass_name, loudnorm, fps=30):
    """One graph: hard-cut video, seamless audio, subtitles last."""
    parts = []
    for i, w in enumerate(wins):
        a0, a1 = w["s"] - w["read_s"], w["e"] - w["read_s"]
        v0, v1 = w["vs"] - w["read_s"], w["ve"] - w["read_s"]
        parts.append(
            f"[{i}:a]asetpts=PTS-STARTPTS,atrim=start={a0:.3f}:end={a1:.3f},"
            f"asetpts=PTS-STARTPTS,aresample=48000,"
            f"aformat=sample_fmts=fltp:channel_layouts=stereo[a{i}]")
        crop_x = w["crop"]
        pre = (f"[{i}:v]setpts=PTS-STARTPTS,trim=start={v0:.3f}:end={v1:.3f},"
               f"setpts=PTS-STARTPTS")
        if mode == "crop":
            parts.append(
                pre + f",scale=-2:{H}:flags=lanczos,"
                f"crop={W}:{H}:x='max(0,min(iw-{W},(iw-{W})*{crop_x}))':y=0,"
                f"unsharp=5:5:0.35:3:3:0.0,setsar=1,fps={fps},format=yuv420p[v{i}]")
        else:
            parts.append(
                pre + f",split=2[a{i}v][b{i}v];"
                f"[a{i}v]scale={W}:{H}:force_original_aspect_ratio=increase,"
                f"crop={W}:{H},boxblur=40:5,eq=brightness=-0.05[bg{i}];"
                f"[b{i}v]scale={W}:-2:flags=lanczos[fg{i}];"
                f"[bg{i}][fg{i}]overlay=(W-w)/2:(H-h)/2,setsar=1,fps={fps},"
                f"format=yuv420p[v{i}]")
    vids = "".join(f"[v{i}]" for i in range(len(wins)))
    auds = "".join(f"[a{i}]" for i in range(len(wins)))
    n = len(wins)
    graph = ";".join(parts) + f";{vids}concat=n={n}:v=1:a=0[vc];" + \
        f"{auds}concat=n={n}:v=0:a=1[ac];"
    if ass_name:
        fonts = FONTS_DIR if any((FONTS_DIR / name).exists() for name in
                                 ("ArchivoBlack-Regular.ttf", "Anton-Regular.ttf")) else None
        opts = f"subtitles={ass_name}"
        if fonts:
            opts += f":fontsdir={fonts}"
        graph += f"[vc]{opts}[v];"
    else:
        graph += "[vc]null[v];"
    graph += "[ac]loudnorm=I=-14:TP=-1.5:LRA=11[a]" if loudnorm else "[ac]anull[a]"
    return graph


def _clean_word(text):
    """Keep the transcript verbatim, but drop sound tags and stray markers."""
    t = text.strip()
    if not t or t.startswith("[") or t in {">>", ">"}:
        return ""
    if not re.search(r"[A-Za-z0-9$]", t):
        return ""
    return t.upper().replace("{", "(").replace("}", ")")


def _caption_chunks(clip_words, max_words, max_chars=16):
    """Group words into short phrase-sized cards, breaking at punctuation."""
    chunks, cur = [], []
    for raw in clip_words:
        if raw.get("break") and cur:
            chunks.append(cur)
            cur = []
        text = _clean_word(raw["w"])
        if not text:
            continue
        item = {"w": text, "s": raw["s"], "e": raw["e"]}
        candidate = cur + [item]
        joined = " ".join(x["w"] for x in candidate)
        if cur and (len(candidate) > max_words or len(joined) > max_chars):
            chunks.append(cur)
            cur = [item]
        else:
            cur = candidate
        if cur and re.search(r"[.!?,;:]$", cur[-1]["w"]) and (
                len(cur) >= 2 or len(cur[-1]["w"]) > 4):
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    return chunks


def _two_lines(text, max_chars=14):
    """Split one caption card into two balanced, centered lines."""
    words = text.split()
    if len(text) <= max_chars or len(words) < 2:
        return text
    first = []
    for word in words:
        if first and len(" ".join(first + [word])) > max_chars:
            break
        first.append(word)
    rest = words[len(first):]
    return " ".join(first) + r"\N" + " ".join(rest) if rest else text


def build_ass(clip_words, style, max_words, path):
    """Build captions. Bold style = heavy font with the spoken word in yellow."""
    if style == "bold":
        if (FONTS_DIR / "ArchivoBlack-Regular.ttf").exists():
            font = "Archivo Black"
        elif (FONTS_DIR / "Anton-Regular.ttf").exists():
            font = "Anton"
        else:
            font = "DejaVu Sans"
        size, outline, shadow, spacing, baseline = 112, 9, 5, 3, 1620
        bold_flag = 0  # the font is already black/heavy
        white, active = "&H00FFFFFF", "&H0000D4FF"  # white / warm yellow
    else:
        font = "Liberation Sans"
        size, outline, shadow, spacing, baseline = 64, 4, 2, 0, 1480
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
Style: Default,{font},{size},{white},{white},&H00000000,&H78000000,{bold_flag},0,0,0,100,100,{spacing},0,1,{outline},{shadow},2,80,80,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    if style != "bold":
        for i in range(0, len(clip_words), max_words):
            chunk = clip_words[i:i + max_words]
            c_start = chunk[0]["s"]
            c_end = chunk[-1]["e"] + 0.2
            if i + max_words < len(clip_words):
                c_end = min(clip_words[i + max_words]["s"], c_end)
            parts = []
            for j, w in enumerate(chunk):
                nxt = chunk[j + 1]["s"] if j + 1 < len(chunk) else w["e"]
                cs = max(1, round((nxt - w["s"]) * 100))
                parts.append(f"{{\\kf{cs}}}{w['w'].replace('{', '(').replace('}', ')')}")
            lines.append(f"Dialogue: 0,{ass_time(c_start)},{ass_time(c_end)},"
                         f"Default,,0,0,0,,{' '.join(parts)}")
        Path(path).write_text(header + "\n".join(lines) + "\n", encoding="utf-8")
        return len(lines)

    chunks = _caption_chunks(clip_words, max_words)
    for ci, chunk in enumerate(chunks):
        c_start = chunk[0]["s"]
        c_end = chunk[-1]["e"] + 0.18
        if ci + 1 < len(chunks):
            c_end = min(chunks[ci + 1][0]["s"], c_end)
        display = _two_lines(" ".join(w["w"] for w in chunk))
        first_line_words = len(display.split(r"\N")[0].split(" "))
        total_words = len(chunk)
        for j, w in enumerate(chunk):
            ev_start = w["s"]
            ev_end = chunk[j + 1]["s"] if j + 1 < len(chunk) else c_end
            if ev_end <= ev_start:
                continue
            rendered = []
            for k, token in enumerate(chunk):
                color = active if k == j else white
                if k == first_line_words and first_line_words < total_words:
                    rendered.append(r"\N")
                elif k > 0:
                    rendered.append(" ")
                rendered.append(f"{{\\c{color}}}{token['w']}")
            anim = ""
            if j == 0 and j == total_words - 1:
                anim += r"\fad(40,90)"
            elif j == 0:
                anim += r"\fad(40,0)"
            elif j == total_words - 1:
                anim += r"\fad(0,90)"
            if j == 0:
                anim += (r"\fscx94\fscy94"
                         r"\t(0,120,\fscx104\fscy104)\t(120,190,\fscx100\fscy100)")
            lines.append(f"Dialogue: 0,{ass_time(ev_start)},{ass_time(ev_end)},"
                         f"Default,,0,0,0,,{{\\an2\\pos(540,{baseline}){anim}}}{''.join(rendered)}")
    Path(path).write_text(header + "\n".join(lines) + "\n", encoding="utf-8")
    return len(lines)


def build_filter(mode, crops, durations, transitions, ass_name, loudnorm, overlap=0.0, fps=30):
    """Build one graph that crops every segment, joins them, then captions."""
    parts = []
    for i, crop_x in enumerate(crops):
        if mode == "crop":
            # full-frame 9:16: scale to cover, crop a 1080-wide window, sharpen a touch
            parts.append(
                f"[{i}:v]scale=-2:{H}:flags=lanczos,"
                f"crop={W}:{H}:x='max(0,min(iw-{W},(iw-{W})*{crop_x}))':y=0,"
                f"unsharp=5:5:0.35:3:3:0.0,setsar=1,fps={fps},format=yuv420p,"
                f"setpts=PTS-STARTPTS[v{i}]")
        else:
            parts.append(
                f"[{i}:v]split=2[a{i}][b{i}];"
                f"[a{i}]scale={W}:{H}:force_original_aspect_ratio=increase,"
                f"crop={W}:{H},boxblur=40:5,eq=brightness=-0.05[bg{i}];"
                f"[b{i}]scale={W}:-2:flags=lanczos[fg{i}];"
                f"[bg{i}][fg{i}]overlay=(W-w)/2:(H-h)/2,setsar=1,fps={fps},"
                f"format=yuv420p,setpts=PTS-STARTPTS[v{i}]")
        parts.append(
            f"[{i}:a]aresample=48000,"
            f"aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"asetpts=PTS-STARTPTS[a{i}]")
    if overlap > 0 and len(crops) > 1:
        video_chain, prev_v = [], "v0"
        audio_chain, prev_a = [], "a0"
        offset = 0.0
        for i in range(1, len(crops)):
            offset += durations[i - 1] - overlap
            transition = transitions[i - 1] if transitions else "fade"
            out_v = f"vx{i}"
            video_chain.append(
                f"[{prev_v}][v{i}]xfade=transition={transition}:"
                f"duration={overlap:.3f}:offset={offset:.3f}[{out_v}]")
            prev_v = out_v
            out_a = f"ax{i}"
            audio_chain.append(f"[{prev_a}][a{i}]acrossfade=d={overlap:.3f}[{out_a}]")
            prev_a = out_a
        graph = ";".join(parts + video_chain + audio_chain) + \
            f";[{prev_v}]null[vc];[{prev_a}]anull[ac];"
    else:
        pairs = "".join(f"[v{i}][a{i}]" for i in range(len(crops)))
        graph = ";".join(parts) + ";" + \
            f"{pairs}concat=n={len(crops)}:v=1:a=1[vc][ac];"
    if ass_name:
        fonts = FONTS_DIR if any((FONTS_DIR / n).exists() for n in
                                 ("ArchivoBlack-Regular.ttf", "Anton-Regular.ttf")) else None
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
    ap.add_argument("--transition-duration", type=float, default=0.0,
                    help="legacy crossfade seconds between segments (0 = hard cuts)")
    ap.add_argument("--transition", default="fade",
                    help="default xfade transition: fade, slideleft, smoothleft, ...")
    ap.add_argument("--jl-lead", type=float, default=0.22,
                    help="seconds a J/L cut leads or lags the picture (default 0.22)")
    ap.add_argument("--lowmem", action="store_true",
                    help="render each segment in its own process (safe with OBS/VS Code)")
    ap.add_argument("--fps", type=int, default=30,
                    help="output frame rate (default 30; use 60 for YouTube uploads)")
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
        plans = plan_segments(clip, words, total, args.max_duration, args.transition)
        base_dur = sum(e - s for s, e, _crop, _transition in plans)
        use_jl = any(t in ("jcut", "lcut") for _s, _e, _c, t in plans)
        xdur = 0.0 if use_jl else max(0.0, args.transition_duration)
        if xdur and len(plans) > 1:
            xdur = min(xdur, min(e - s for s, e, _c, _t in plans) / 3)
        dur = base_dur - xdur * max(0, len(plans) - 1)
        if dur < 3:
            print(f"skip clip {i}: too short ({dur:.1f}s)", file=sys.stderr)
            continue
        idx = int(clip.get("index", i))
        name = f"{idx:02d}_{slug(clip.get('title', 'clip'))}"
        mp4 = out / f"{name}.mp4"
        ass_name = None
        if captions:
            ass_name = f"{name}.ass"
            tw = timeline_words(words, plans, xdur)
            n = build_ass(tw, args.caption_style, args.max_words, out / ass_name)
            if n == 0:
                ass_name = None
        mode = clip.get("reframe", args.reframe)
        if use_jl:
            wins = jl_windows(plans, args.jl_lead)
            fc = build_jl_filter(mode, wins, ass_name, not args.no_loudnorm)
        else:
            crops = [crop for _s, _e, crop, _t in plans]
            durations = [e - s for s, e, _c, _t in plans]
            transitions = [t for _s, _e, _c, t in plans]
            fc = build_filter(mode, crops, durations, transitions, ass_name,
                              not args.no_loudnorm, xdur)

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

        seg_note = " + ".join(f"{s:.1f}-{e:.1f}" for s, e, _crop, _t in plans)
        lowmem = use_jl and args.lowmem
        print(f"[{i}/{len(spec['clips'])}] {name}  {seg_note}s "
              f"({dur:.1f}s, {mode}, {res_note}{', lowmem' if lowmem else ''})")
        if lowmem:
            started = time.time()
            code = render_lowmem(wins, mode, source, out, name, mp4, ass_name,
                                 prefix, threads, args.preset, args.crf, dur,
                                 not args.no_loudnorm, args.fps)
            err, took = "", time.time() - started
        else:
            cmd = prefix + ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                   "-nostats", "-progress", "pipe:1"]
            if use_jl:
                for w in wins:
                    cmd += ["-ss", f"{w['read_s']:.3f}",
                            "-t", f"{w['read_e'] - w['read_s']:.3f}", "-i", str(source)]
            else:
                for s, e, _crop, _transition in plans:
                    cmd += ["-ss", f"{s:.3f}", "-t", f"{e - s:.3f}", "-i", str(source)]
            cmd += ["-filter_complex", fc, "-threads", str(threads),
                    "-map", "[v]", "-map", "[a]",
                    "-c:v", "libx264", "-crf", str(args.crf), "-preset", args.preset,
                    "-pix_fmt", "yuv420p", "-r", "30", "-c:a", "aac", "-b:a", "160k",
                    "-ar", "48000", "-movflags", "+faststart", mp4.name]
            # run inside out/ so the subtitles filter can use a plain relative filename
            code, err, took = run_ffmpeg(cmd, out, dur, name)
        if code != 0:
            print(err[-1500:], file=sys.stderr)
            if "subtitles" in err.lower() or "libass" in err.lower():
                print("Your ffmpeg may lack libass. Use a full ffmpeg build or --no-captions.", file=sys.stderr)
            continue
        print(f"  ✓ {name} rendered in {fmt_time(took)}")
        record = {"index": idx, "queue": i, "file": str(mp4),
                  "segments": [{"start": round(s, 2), "end": round(e, 2),
                                "crop_x": round(c, 2), "transition": t}
                               for s, e, c, t in plans],
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
