"""Turn a directory of downloads into a Hugging Face AudioFolder split.

The downloader writes one directory per video::

    <download_root>/<group>/<video_id>/<video_id>.webm
    <download_root>/<group>/<video_id>/metadata.json

`datasets` expects a flat split instead, with a manifest CSV beside the audio::

    train/audio/<video_id>.webm
    train/metadata.csv          # first column MUST be file_name

`file_name` is the column `datasets` matches on; the rest is free-form metadata that
becomes dataset columns. Everything is stringified on the way out because a CSV has no
types and a half-typed column loads worse than a consistently-stringified one.
"""
from __future__ import annotations

import csv
import json
import shutil
from pathlib import Path

# Ordered by preference: whichever the downloader actually produced is what we ship.
AUDIO_SUFFIXES = (".opus", ".webm", ".m4a", ".mp3", ".ogg", ".wav", ".flac", ".aac")

# file_name first - `datasets` looks for it and some loaders assume column 0.
COLUMNS = [
    "file_name",
    "video_id",
    "title",
    "url",
    "duration",
    "channel",
    "channel_id",
    "view_count",
    "tags",
    "audio_format",
]


def find_audio(video_dir: Path) -> Path | None:
    """The finished audio file in a video directory, if there is one.

    Ignores yt-dlp's ``.part`` and ``.ytdl`` scratch files, so a download interrupted
    mid-write is never mistaken for a complete one.
    """
    for suffix in AUDIO_SUFFIXES:
        for candidate in sorted(video_dir.glob(f"*{suffix}")):
            if candidate.is_file() and candidate.stat().st_size > 0:
                return candidate
    return None


def _text(value: object) -> str:
    """Flatten any JSON value to something a CSV cell can hold."""
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _row_for(video_id: str, audio: Path, source: dict) -> dict[str, str]:
    return {
        "file_name": f"audio/{audio.name}",
        "video_id": video_id,
        "title": _text(source.get("title")),
        "url": _text(source.get("video_url")),
        "duration": _text(source.get("duration")),
        "channel": _text(source.get("channel_title") or source.get("channel")),
        "channel_id": _text(source.get("channel_id")),
        "view_count": _text(source.get("view_count")),
        "tags": _text(source.get("tags") or []),
        "audio_format": audio.suffix.lstrip(".").lower(),
    }


def build_split(group_dir: Path, split: str = "train") -> Path:
    """Collect finished downloads in ``group_dir`` into ``group_dir/<split>/``.

    Returns the split directory. Videos without a finished audio file are skipped
    silently - they are failures the caller already knows about from the state DB.
    """
    split_dir = group_dir / split
    audio_dir = split_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, str]] = []
    for video_dir in sorted(p for p in group_dir.iterdir() if p.is_dir() and p.name != split):
        audio = find_audio(video_dir)
        if audio is None:
            continue

        meta_path = video_dir / "metadata.json"
        source: dict = {}
        if meta_path.exists():
            try:
                source = json.loads(meta_path.read_text(encoding="utf-8")).get("source_row", {})
            except json.JSONDecodeError:
                source = {}
        video_id = source.get("video_id") or video_dir.name

        target = audio_dir / f"{video_id}{audio.suffix.lower()}"
        # Copy rather than move: the group directory is deleted wholesale afterwards,
        # and a copy keeps the original intact if the upload fails and we retry.
        if not target.exists() or target.stat().st_size != audio.stat().st_size:
            shutil.copy2(audio, target)

        rows.append(_row_for(video_id, target, source))

    write_metadata_csv(split_dir / "metadata.csv", rows)
    return split_dir


def read_metadata_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    # utf-8-sig: Excel writes a BOM, and a BOM left in place corrupts the first
    # column name into "﻿file_name", which `datasets` then fails to find.
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_metadata_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in COLUMNS})


def merge_metadata(existing: list[dict[str, str]], new: list[dict[str, str]]) -> list[dict[str, str]]:
    """Union of two metadata row lists, keyed on file_name, new rows winning.

    The dataset carries ONE metadata.csv describing every file uploaded so far, so each
    batch has to be merged into the running file rather than replacing it.
    """
    merged = {row["file_name"]: row for row in existing if row.get("file_name")}
    for row in new:
        if row.get("file_name"):
            merged[row["file_name"]] = row
    return list(merged.values())
