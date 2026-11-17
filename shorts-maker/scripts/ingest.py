#!/usr/bin/env python3
"""Download a video with yt-dlp as source.mp4. Use only for content you have rights to."""
import argparse, subprocess, sys, shutil
from pathlib import Path


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("url")
    ap.add_argument("--out", default="shorts_work", help="output folder")
    ap.add_argument("--max-height", type=int, default=1080)
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    exe = shutil.which("yt-dlp")
    cmd = [exe] if exe else [sys.executable, "-m", "yt_dlp"]
    cmd += [
        "-f", f"bv*[height<={args.max_height}]+ba/b[height<={args.max_height}]",
        "--merge-output-format", "mp4",
        "-o", str(out / "source.%(ext)s"),
        "--no-playlist",
        args.url,
    ]
    r = subprocess.run(cmd)
    if r.returncode != 0:
        sys.exit("yt-dlp failed. Is it installed (pip install yt-dlp)? Is the URL reachable?")
    print(out / "source.mp4")


if __name__ == "__main__":
    main()
