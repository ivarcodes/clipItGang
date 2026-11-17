#!/usr/bin/env python3
"""Remove every silent gap from the segments of clips.json.

Uses ffmpeg silencedetect on the source audio and the word-level transcript to
tell silence from quiet speech. Each story beat becomes several short, exactly
trimmed sub-segments (`"snap": false`) with no lead-in or tail. The transition
on the last sub-segment of a beat alternates between J-cut and L-cut, so picture
and sound split at every beat instead of dissolving.

Usage:
    python3 scripts/strip_silence.py clips.json --transcript transcript.json \
        --out clips_silent.json
"""
import argparse, json, re, subprocess, sys
from pathlib import Path

PAT = re.compile(
    r"silence_start: ([0-9.]+).*?silence_end: ([0-9.]+) \| silence_duration: ([0-9.]+)",
    re.S)


def silences(source, start, dur, threshold=-33, min_dur=0.13):
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{start:.3f}",
           "-t", f"{dur:.3f}", "-i", str(source), "-vn",
           "-af", f"silencedetect=noise={threshold}dB:d={min_dur}",
           "-f", "null", "-"]
    p = subprocess.run(cmd, capture_output=True, text=True)
    return [(start + float(a), start + float(b), float(d))
            for a, b, d in PAT.findall(p.stderr)]


def silent_parts(a, b, starts, min_cut=0.13):
    """Split a detected silence around words that actually start inside it."""
    words = [s for s in starts if a - 0.02 <= s <= b + 0.02]
    out, cursor = [], a
    for p in words:
        if p - cursor >= min_cut:
            out.append((cursor, p))
        cursor = max(cursor, p + 0.05)
    if b - cursor >= min_cut:
        out.append((cursor, b))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("clips", help="clips.json with the story beats")
    ap.add_argument("--transcript", required=True, help="word-level transcript.json")
    ap.add_argument("--source", default=None, help="override the source video")
    ap.add_argument("--out", default="clips_silent.json")
    ap.add_argument("--threshold", type=float, default=-33, help="silencedetect dB")
    ap.add_argument("--min-gap", type=float, default=0.13,
                    help="only remove silent gaps at least this long (default 0.13)")
    ap.add_argument("--min-part", type=float, default=0.12,
                    help="keep sub-segments at least this long (default 0.12)")
    args = ap.parse_args()

    spec = json.loads(Path(args.clips).read_text())
    source = Path(args.source or spec.get("source", ""))
    if not source.is_file():
        sys.exit(f"Source video not found: {source}")
    starts = [w["s"] for w in
              json.loads(Path(args.transcript).read_text()).get("words", [])]

    min_gap, min_part = args.min_gap, args.min_part
    total = removed = 0.0
    for clip in spec["clips"]:
        out = []
        for beat, seg in enumerate(clip.get("segments") or [
                {"start": clip.get("start"), "end": clip.get("end"),
                 "crop_x": clip.get("crop_x", 0.5)}]):
            s, e = float(seg["start"]), float(seg["end"])
            cuts = []
            for a, b, d in silences(source, s, e - s, args.threshold):
                if d >= min_gap:
                    cuts.extend(silent_parts(a, b, starts, min_gap))
            merged = []
            for a, b in sorted(cuts):
                if merged and a <= merged[-1][1] + 0.02:
                    merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
                else:
                    merged.append((a, b))
            cursor, parts = s, []
            for a, b in merged:
                if a - cursor >= min_part:
                    parts.append((cursor, a))
                removed += b - a
                cursor = b
            if e - cursor >= min_part:
                parts.append((cursor, e))
            for j, (ps, pe) in enumerate(parts):
                # the transition on the last part of a beat splits the next beat
                transition = seg.get("transition", "cut") if j == len(parts) - 1 else "cut"
                item = {"beat": beat, "start": round(ps, 3), "end": round(pe, 3),
                        "crop_x": seg.get("crop_x", 0.5),
                        "transition": transition, "snap": False}
                out.append(item)
            total += sum(pe - ps for ps, pe in parts)
        # alternate J-cut / L-cut at beat boundaries; final segment is a plain cut
        last_by_beat = {}
        for i, seg in enumerate(out):
            last_by_beat[seg["beat"]] = i
        for n, idx in enumerate(sorted(last_by_beat.values())):
            if idx != len(out) - 1:
                out[idx]["transition"] = "jcut" if n % 2 == 0 else "lcut"
        out[-1]["transition"] = "cut"
        clip["segments"] = out
    Path(args.out).write_text(json.dumps(spec, indent=2))
    print(f"{total:.1f}s of speech in {sum(len(c['segments']) for c in spec['clips'])} "
          f"sub-segments; removed {removed:.1f}s of silence -> {args.out}")


if __name__ == "__main__":
    main()
