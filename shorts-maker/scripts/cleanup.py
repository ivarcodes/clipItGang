#!/usr/bin/env python3
"""Remove temporary transcription/render junk after clips are finished.

Two modes:
  default  remove known working artifacts (chunk cache, temp wavs, logs)
  --final  remove the whole working set so only clips/ and reference/ remain
           (plus the shorts-maker tooling, .venv and prompt.txt)

Final outputs (clips/*.mp4, clips/*.ass, clips/manifest.json) and the source
video are never touched. Use --dry-run to preview.

Usage:
    python3 shorts-maker/scripts/cleanup.py            # clean working junk
    python3 shorts-maker/scripts/cleanup.py --final    # project-level clean
    python3 shorts-maker/scripts/cleanup.py --dry-run --final

`render_clips.py --clean` runs the default clean after all clips render.
The script refuses to run while transcription or rendering is active.
"""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

# working artifacts, relative to the project root
TARGETS = [
    "transcriptions/._chunks",
    "transcriptions/._slice.wav",
    "transcriptions/transcribe.log",
    "transcriptions/transcribe.pid",
    "transcriptions/clip_pass/._slice.wav",
    "transcriptions/clip_pass/transcript_small.json",
    "samples/base_test",
    "samples/chunk_test",
]

# everything that is not a final deliverable
FINAL_TARGETS = [
    "transcriptions",
    "samples",
    "clips.json",
    "clips_silent.json",
    "clips_test.json",
    "shorts_work",
    "nohup.out",
]
FINAL_GLOBS = ["*.log", "*.pid", "*.tmp", "*~"]


def path_size(path):
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for dirpath, _, files in os.walk(path):
        for name in files:
            try:
                total += (Path(dirpath) / name).stat().st_size
            except OSError:
                pass
    return total


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}"
        n /= 1024


def job_running(pattern):
    try:
        r = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
        return bool(r.stdout.strip())
    except FileNotFoundError:
        return False


def remove_targets(root, targets, dry_run):
    found, freed = [], 0
    for target in targets:
        target = target.resolve()
        if root not in target.parents and target != root:
            continue  # safety: never step outside the project
        if not target.exists():
            continue
        size = path_size(target)
        found.append((target, size))
        freed += size
        if dry_run:
            continue
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
    return found, freed


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=".", help="project root (default: current dir)")
    ap.add_argument("--dry-run", action="store_true", help="only list what would be removed")
    ap.add_argument("--final", action="store_true",
                    help="remove everything except clips/ and reference/")
    ap.add_argument("--force", action="store_true", help="clean even if a job is running")
    args = ap.parse_args()

    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        sys.exit(f"Not a directory: {root}")
    if not (root / "transcriptions").is_dir() and not (root / "reference").is_dir():
        sys.exit(f"{root} does not look like a shorts project (no transcriptions/ or reference/).")

    if not args.force:
        if job_running("shorts-maker/scripts/transcribe.py"):
            sys.exit("A transcription job is still running; refusing to delete its chunks. "
                     "Wait for it to finish or pass --force.")
        if job_running("shorts-maker/scripts/render_clips.py"):
            sys.exit("A render is still running; refusing to clean. "
                     "Wait for it to finish or pass --force.")

    if args.final:
        clips = list((root / "clips").glob("*.mp4"))
        if not clips:
            sys.exit("No rendered clips in clips/; refusing final cleanup.")
        targets = [root / rel for rel in FINAL_TARGETS]
        targets += [p for pattern in FINAL_GLOBS for p in root.glob(pattern)]
        keep = ["reference/", "clips/", "shorts-maker/", ".venv/", "prompt.txt"]
        print("Final cleanup keeps: " + ", ".join(keep))
    else:
        targets = [root / rel for rel in TARGETS]

    found, freed = remove_targets(root, targets, args.dry_run)
    verb = "Would remove" if args.dry_run else "Removed"
    print(f"{verb} {len(found)} item(s), {human(freed)} total")
    for target, size in found:
        print(f"  {human(size):>9}  {target.relative_to(root)}")
    if not found:
        print("Nothing to clean.")


if __name__ == "__main__":
    main()
