"""Manifest I/O.

A manifest is a JSONL file: one JSON object per line, one line per video. The only
required key is ``video_id``; ``video_url`` is derived from it when absent. Everything
else (title, duration, channel, ...) is carried through untouched and ends up as a
column in the dataset's ``metadata.csv``.

JSONL rather than a single JSON array so a 9,000-row manifest streams instead of
loading whole, and so a truncated file still yields every complete line before the cut.
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

WATCH_URL = "https://www.youtube.com/watch?v={}"


def iter_manifest(path: Path) -> Iterator[dict]:
    """Yield each row of a JSONL manifest, skipping blank lines.

    Read as utf-8-sig because a manifest hand-edited on Windows arrives with a BOM, and
    a BOM on line 1 otherwise fails the whole file with an opaque JSON error.
    """
    with path.open(encoding="utf-8-sig") as handle:
        for number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{number}: not valid JSON - {exc}") from exc
            if "video_id" not in row:
                raise ValueError(f"{path}:{number}: row has no 'video_id'")
            row.setdefault("video_url", WATCH_URL.format(row["video_id"]))
            yield row


def read_manifest(path: Path) -> list[dict]:
    if not path.exists():
        raise SystemExit(f"Manifest not found: {path}")
    return list(iter_manifest(path))


def write_manifest(path: Path, rows: list[dict]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return len(rows)


def read_json(path: Path, default: object = None) -> object:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
