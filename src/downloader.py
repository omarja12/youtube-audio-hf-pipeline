"""Download audio for every row of a manifest, recording the outcome in a state DB.

Runs as its own process (see ``pipeline.py``), for two reasons:

* yt-dlp is the part that breaks. Extractors raise from threads, and a bad video can
  wedge or abort the interpreter. A subprocess boundary means the orchestrator survives
  anything that happens here and can report it.
* yt-dlp needs a JS runtime on PATH to solve YouTube's challenge. Injecting that into a
  child environment is clean; mutating the parent's PATH is not.

Every outcome - success or failure, with the error text - is written to a SQLite table.
That table is the only channel back to the orchestrator, which classifies the errors to
decide whether a video is permanently gone, the network is flaky, or the IP is blocked.

    python -m src.downloader --manifest m.jsonl --download-root dl --state-db state.sqlite
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections.abc import Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from yt_dlp import YoutubeDL

if __package__ in (None, ""):  # allow `python src/downloader.py` as well as `-m`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from src.manifest import read_manifest, write_json
else:
    from .manifest import read_manifest, write_json

SCHEMA = """
CREATE TABLE IF NOT EXISTS downloads (
    video_id     TEXT PRIMARY KEY,
    video_url    TEXT NOT NULL,
    group_name   TEXT,
    status       TEXT NOT NULL,
    local_path   TEXT,
    file_size    INTEGER,
    attempts     INTEGER NOT NULL DEFAULT 0,
    completed_at TEXT,
    error        TEXT
);
"""

UPSERT = """
INSERT INTO downloads (video_id, video_url, group_name, status, local_path,
                       file_size, attempts, completed_at, error)
VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
ON CONFLICT(video_id) DO UPDATE SET
    group_name   = excluded.group_name,
    status       = excluded.status,
    local_path   = excluded.local_path,
    file_size    = excluded.file_size,
    attempts     = downloads.attempts + 1,
    completed_at = excluded.completed_at,
    error        = excluded.error
"""

# Only the unambiguous transport failures, used for the circuit breaker below. Anything
# YouTube says about the *video* is left for the orchestrator to classify - see
# src/classify.py, which is where the consequential decisions are made.
TRANSPORT_ERRORS = (
    "getaddrinfo", "temporary failure in name resolution", "network is unreachable",
    "failed to establish a new connection", "connection aborted", "connection reset",
    "timed out", "timeout",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_transport_error(error: str | None) -> bool:
    if not error:
        return False
    lowered = error.lower()
    return any(marker in lowered for marker in TRANSPORT_ERRORS)


def open_state_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    # WAL so the orchestrator can read progress while this process is still writing.
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(SCHEMA)
    connection.commit()
    return connection


def ydl_options(args: argparse.Namespace, target: Path, video_id: str) -> dict[str, Any]:
    options: dict[str, Any] = {
        "outtmpl": str(target / f"{video_id}.%(ext)s"),
        "format": args.format,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "ignoreerrors": False,
        "socket_timeout": args.socket_timeout,
        "retries": args.retries,
        "extractor_retries": args.retries,
    }
    if args.cookies:
        options["cookiefile"] = args.cookies
    if args.force_ipv4:
        options["source_address"] = "0.0.0.0"
    return options


def download_one(row: dict, args: argparse.Namespace, download_root: Path) -> dict:
    """Download one video. Never raises - failures come back as a result dict."""
    video_id = row["video_id"]
    target = download_root / args.group_name / video_id
    target.mkdir(parents=True, exist_ok=True)

    result = {"row": row, "status": "error", "path": None, "size": None, "error": None}
    try:
        with YoutubeDL(ydl_options(args, target, video_id)) as ydl:
            info = ydl.extract_info(row["video_url"], download=True)

        requested = (info or {}).get("requested_downloads") or []
        path = requested[0].get("filepath") if requested else None
        if not path:
            path = str(target / f"{video_id}.{(info or {}).get('ext', 'bin')}")

        if Path(path).exists():
            result.update(status="ok", path=path, size=Path(path).stat().st_size)
            # Keep the manifest row next to the audio: audiofolder.py reads it back to
            # build metadata.csv, so the dataset's columns survive a restart.
            write_json(target / "metadata.json", {"source_row": row})
        else:
            result["error"] = "yt-dlp reported success but no file was written"
    except Exception as exc:  # noqa: BLE001 - the error text IS the product here
        result["error"] = str(exc)[:4000]
    finally:
        # Politeness delay. Removing it is the fastest way to get the IP rate-limited.
        if args.sleep_seconds > 0:
            time.sleep(args.sleep_seconds)
    return result


def iter_results(rows: list[dict], args: argparse.Namespace, download_root: Path) -> Iterator[dict]:
    """Run downloads with bounded concurrency, yielding each result as it lands.

    Submitting all rows up front would build a queue of thousands of futures that cannot
    be cancelled when the circuit breaker trips; keeping only ``workers * 2`` in flight
    means stopping early actually stops early.
    """
    pending_rows = iter(rows)
    in_flight: set[Future[dict]] = set()

    def submit(executor: ThreadPoolExecutor) -> bool:
        row = next(pending_rows, None)
        if row is None:
            return False
        in_flight.add(executor.submit(download_one, row, args, download_root))
        return True

    with ThreadPoolExecutor(max_workers=args.workers, thread_name_prefix="dl") as executor:
        for _ in range(min(args.workers * 2, len(rows))):
            submit(executor)
        while in_flight:
            done, still_pending = wait(in_flight, return_when=FIRST_COMPLETED)
            in_flight = set(still_pending)
            for future in done:
                yield future.result()
                submit(executor)


def record(connection: sqlite3.Connection, result: dict, group_name: str) -> None:
    row = result["row"]
    connection.execute(UPSERT, (
        row["video_id"], row["video_url"], group_name, result["status"],
        result["path"], result["size"], utc_now(), result["error"],
    ))


def emit(payload: dict) -> None:
    """One JSON object per line on stdout - the orchestrator tees this to its log."""
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--download-root", required=True)
    parser.add_argument("--state-db", required=True)
    parser.add_argument("--group-name", default="group")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--sleep-seconds", type=float, default=3.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--socket-timeout", type=float, default=120.0)
    parser.add_argument("--format", default="bestaudio/best")
    parser.add_argument("--cookies")
    parser.add_argument("--force-ipv4", action="store_true")
    parser.add_argument("--max-consecutive-network-errors", type=int, default=6)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    rows = read_manifest(Path(args.manifest).resolve())
    download_root = Path(args.download_root).resolve()
    download_root.mkdir(parents=True, exist_ok=True)

    connection = open_state_db(Path(args.state_db).resolve())
    already_done = {
        video_id for (video_id,) in
        connection.execute("SELECT video_id FROM downloads WHERE status = 'ok'")
    }
    todo = [row for row in rows if row["video_id"] not in already_done]

    emit({"event": "start", "manifest_rows": len(rows), "already_ok": len(rows) - len(todo),
          "pending": len(todo), "workers": args.workers})

    processed = 0
    consecutive_transport_errors = 0
    for result in iter_results(todo, args, download_root):
        record(connection, result, args.group_name)
        processed += 1

        if result["status"] == "ok" or not is_transport_error(result["error"]):
            consecutive_transport_errors = 0
        else:
            consecutive_transport_errors += 1

        if processed == 1 or processed % 10 == 0:
            connection.commit()

        # Circuit breaker: a run of pure transport failures means the network is gone,
        # and continuing just burns through the manifest marking good videos as failed.
        if 0 < args.max_consecutive_network_errors <= consecutive_transport_errors:
            connection.commit()
            emit({"event": "stopped", "reason": "network",
                  "consecutive_network_errors": consecutive_transport_errors})
            break

    connection.commit()
    ok = connection.execute("SELECT COUNT(*) FROM downloads WHERE status = 'ok'").fetchone()[0]
    connection.close()
    emit({"event": "done", "processed": processed, "ok_in_state_db": ok})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
