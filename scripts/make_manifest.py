"""Build a manifest from any YouTube playlist or channel URL.

Metadata only - no audio is downloaded, so this is fast and cheap even for a channel
with thousands of videos.

    python scripts/make_manifest.py --url "https://www.youtube.com/@channel/videos" \
        --limit 100 --out examples/my-manifest.jsonl
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from yt_dlp import YoutubeDL

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.manifest import WATCH_URL, write_manifest  # noqa: E402


def entries(url: str, limit: int | None) -> list[dict]:
    options = {
        "quiet": True,
        "no_warnings": True,
        # Flat extraction: list the playlist without resolving each video's formats.
        # Resolving would take hours on a large channel and is not needed here.
        "extract_flat": "in_playlist",
        "skip_download": True,
    }
    if limit:
        options["playlistend"] = limit

    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)
    return [entry for entry in (info or {}).get("entries") or [] if entry]


def to_row(entry: dict) -> dict:
    video_id = entry.get("id")
    return {
        "video_id": video_id,
        "video_url": entry.get("url") or WATCH_URL.format(video_id),
        "title": entry.get("title") or "",
        "duration": int(entry.get("duration") or 0),
        "channel_title": entry.get("channel") or entry.get("uploader") or "",
        "channel_id": entry.get("channel_id") or "",
        "view_count": entry.get("view_count") or "",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="playlist or channel URL")
    parser.add_argument("--out", required=True, help="output .jsonl path")
    parser.add_argument("--limit", type=int, help="stop after N videos")
    parser.add_argument("--min-duration", type=int, default=0, help="drop videos shorter than N seconds")
    parser.add_argument("--max-duration", type=int, default=0, help="drop videos longer than N seconds")
    args = parser.parse_args()

    rows = [to_row(entry) for entry in entries(args.url, args.limit)]
    rows = [row for row in rows if row["video_id"]]

    if args.min_duration:
        rows = [row for row in rows if row["duration"] >= args.min_duration]
    if args.max_duration:
        rows = [row for row in rows if 0 < row["duration"] <= args.max_duration]

    if not rows:
        print("No videos matched.", file=sys.stderr)
        return 1

    count = write_manifest(Path(args.out), rows)
    hours = sum(row["duration"] for row in rows) / 3600
    print(f"{count} videos -> {args.out}  ({hours:.1f} hours of audio)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
