"""Download a manifest of YouTube videos into a Hugging Face audio dataset, in batches.

The unit of work is a *group* of N videos. For each group:

    reset -> download (subprocess) -> build AudioFolder split -> merge metadata
          -> upload to the Hub -> commit -> delete local audio -> next group

Small groups mean frequent safe points and a bounded loss when something goes wrong: an
interrupted run costs at most the current group, never the whole job.

The source of truth for "already uploaded" is the running metadata CSV, and a video only
enters it AFTER its commit succeeds. So the failure modes are all safe - a crash mid
download loses nothing, and a crash mid upload leaves the video simply not-yet-recorded,
to be redone next run. There is no state in which a video is recorded but absent.

    python -m src.pipeline --status         # progress, then exit
    python -m src.pipeline                  # one group, then stop
    python -m src.pipeline --go             # everything remaining
    python -m src.pipeline --go --batch 25  # bigger groups
"""
from __future__ import annotations

import argparse
import atexit
import contextlib
import ctypes
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from huggingface_hub import CommitOperationAdd, HfApi
from huggingface_hub.utils import disable_progress_bars
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.text import Text

if __package__ in (None, ""):  # allow `python src/pipeline.py` as well as `-m`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.audiofolder import build_split, merge_metadata, read_metadata_csv, write_metadata_csv
from src.classify import summarise
from src.manifest import read_json, read_manifest, write_json

ROOT = Path(__file__).resolve().parent.parent
CONSOLE = Console(legacy_windows=False, safe_box=True, highlight=False)
_PROGRESS: Progress | None = None

disable_progress_bars()  # silence huggingface_hub's own tqdm; this module draws its own

for _stream in (sys.stdout, sys.stderr):
    # Windows consoles default to cp1252; Arabic titles in the log would crash the run.
    with contextlib.suppress(AttributeError, ValueError, OSError):
        _stream.reconfigure(encoding="utf-8", errors="replace")


# --------------------------------------------------------------------- config

@dataclass
class Config:
    """Everything that differs between one run of this tool and another."""

    repo_id: str
    manifest: Path
    workspace: Path
    private: bool = True
    batch: int = 4
    max_retry: int = 3
    cookies: Path | None = None
    js_runtime_bin: Path | None = None
    download: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path, overrides: dict) -> Config:
        if not path.exists():
            raise SystemExit(
                f"No config at {path}\n"
                f"Copy config.example.json to {path.name} and set repo_id + manifest."
            )
        # utf-8-sig: Notepad and PowerShell's Set-Content both write a BOM, and a BOM
        # makes json.loads fail with a message that explains nothing to the user.
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        raw.update({key: value for key, value in overrides.items() if value is not None})

        if not raw.get("repo_id"):
            raise SystemExit("config: 'repo_id' is required, e.g. \"your-name/your-dataset\"")

        def as_path(value: object) -> Path | None:
            if not value:
                return None
            candidate = Path(str(value)).expanduser()
            return candidate if candidate.is_absolute() else (ROOT / candidate)

        return cls(
            repo_id=raw["repo_id"],
            manifest=as_path(raw.get("manifest", "examples/sample-manifest.jsonl")),
            workspace=as_path(raw.get("workspace", "workspace")),
            private=bool(raw.get("private", True)),
            batch=int(raw.get("batch", 4)),
            max_retry=int(raw.get("max_retry", 3)),
            cookies=as_path(raw.get("cookies")),
            js_runtime_bin=as_path(raw.get("js_runtime_bin")),
            download=raw.get("download", {}),
        )

    # The run name namespaces every state file, so several manifests can share a
    # workspace without overwriting each other's progress.
    @property
    def run_name(self) -> str:
        return self.manifest.stem

    @property
    def download_root(self) -> Path:
        return self.workspace / "downloads"

    @property
    def group_dir(self) -> Path:
        return self.download_root / self.run_name

    @property
    def state_db(self) -> Path:
        return self.workspace / "state" / "downloads.sqlite"

    @property
    def metadata_csv(self) -> Path:
        return self.workspace / f"{self.run_name}-metadata.csv"

    @property
    def gone_file(self) -> Path:
        return self.workspace / "state" / f"{self.run_name}-gone.json"

    @property
    def retry_file(self) -> Path:
        return self.workspace / "state" / f"{self.run_name}-retry.json"

    @property
    def log_file(self) -> Path:
        return self.workspace / f"{self.run_name}.log"

    def download_args(self) -> list[str]:
        """Defaults tuned against YouTube, not against the local machine.

        4 workers with a 3s delay is the ceiling before YouTube starts returning fake
        "unavailable" for good videos - i.e. pushing these higher yields LESS data.
        """
        defaults = {
            "workers": 4,
            "sleep-seconds": 3,
            "retries": 2,
            "socket-timeout": 120,
            "max-consecutive-network-errors": 6,
            "force-ipv4": True,
        }
        defaults.update({key.replace("_", "-"): value for key, value in self.download.items()})

        args: list[str] = []
        for key, value in defaults.items():
            if isinstance(value, bool):
                if value:
                    args.append(f"--{key}")
            else:
                args += [f"--{key}", str(value)]
        return args


# --------------------------------------------------------------------- display

def make_progress() -> Progress:
    """One transient bar: it vanishes when done, leaving only the permanent OK lines."""
    return Progress(
        SpinnerColumn(spinner_name="dots12", style="bold yellow"),
        TextColumn("[bold]{task.description}"),
        BarColumn(bar_width=46, style="grey35",
                  complete_style="bold bright_cyan", finished_style="bold bright_green",
                  pulse_style="bold bright_magenta"),
        TextColumn("[bold bright_white]{task.percentage:>3.0f}%"),
        TextColumn("[bright_white]{task.completed:>3.0f}/{task.total:<3.0f}"),
        TextColumn("{task.fields[note]}"),
        TimeElapsedColumn(),
        console=CONSOLE, transient=True, refresh_per_second=10,
    )


def say(message: str) -> None:
    """Print above the live bar, so output is not chewed up by the animation."""
    (_PROGRESS.console if _PROGRESS is not None else CONSOLE).print(message)


def log(config: Config, message: str) -> None:
    config.log_file.parent.mkdir(parents=True, exist_ok=True)
    with config.log_file.open("a", encoding="utf-8") as handle:
        handle.write(Text.from_markup(message).plain + "\n")
    say(message)


# --------------------------------------------------------- keep the machine awake

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_AWAYMODE_REQUIRED = 0x00000040


def keep_awake(on: bool = True) -> bool:
    """Stop Windows sleeping mid-run. Released on exit.

    A long run is mostly network wait with no keyboard or mouse activity, which Windows
    counts as idle - the default is to suspend after 20 minutes, killing the run.

    Blocks sleep only; the screen may still turn off, which is fine. Closing a laptop
    lid still sleeps the machine: Windows honours the lid switch regardless.
    No-op on other platforms (use ``caffeinate`` on macOS, ``systemd-inhibit`` on Linux).
    """
    if os.name != "nt":
        return False
    try:
        set_state = ctypes.windll.kernel32.SetThreadExecutionState
    except (AttributeError, OSError):
        return False
    if not on:
        set_state(ES_CONTINUOUS)
        return False
    if set_state(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED):
        return True
    return bool(set_state(ES_CONTINUOUS | ES_SYSTEM_REQUIRED))


# --------------------------------------------------------------------- state

def uploaded_ids(config: Config) -> set[str]:
    """Video ids already committed to the Hub, read from the running metadata CSV."""
    return {
        Path(row["file_name"]).stem
        for row in read_metadata_csv(config.metadata_csv)
        if row.get("file_name")
    }


def state_rows(config: Config, video_ids: set[str]) -> list[tuple[str, str, str]]:
    """Read ``(video_id, status, error)`` for a group, read-only and never fatal."""
    if not config.state_db.exists() or not video_ids:
        return []
    try:
        connection = sqlite3.connect(f"file:{config.state_db}?mode=ro", uri=True, timeout=5)
        placeholders = ",".join("?" * len(video_ids))
        rows = connection.execute(
            f"SELECT video_id, status, COALESCE(error, '') FROM downloads "
            f"WHERE video_id IN ({placeholders})",
            tuple(video_ids),
        ).fetchall()
        connection.close()
        return rows
    except sqlite3.Error:
        return []  # the writer holds the lock; the next poll will pick it up


def finished_downloads(config: Config) -> int:
    """Video directories holding a COMPLETE audio file (not a .part still being written)."""
    if not config.group_dir.exists():
        return 0
    from src.audiofolder import find_audio
    return sum(
        1 for directory in config.group_dir.iterdir()
        if directory.is_dir() and directory.name != "train" and find_audio(directory)
    )


def reset_group(config: Config, video_ids: list[str]) -> None:
    """Start a group from a clean slate: no leftover files, no stale state rows.

    Re-downloading a handful of already-fetched videos is cheap; reasoning about a
    half-populated directory is not.
    """
    shutil.rmtree(config.group_dir, ignore_errors=True)
    if not config.state_db.exists() or not video_ids:
        return
    try:
        connection = sqlite3.connect(config.state_db, timeout=15)
        placeholders = ",".join("?" * len(video_ids))
        connection.execute(
            f"DELETE FROM downloads WHERE video_id IN ({placeholders})", tuple(video_ids)
        )
        connection.commit()
        connection.close()
    except sqlite3.Error:
        pass


# --------------------------------------------------------------------- download

def child_env(config: Config) -> dict:
    """Environment for the downloader, with the JS runtime on PATH.

    YouTube serves a JavaScript challenge that yt-dlp must execute. Without a runtime
    (Deno) on PATH, every download fails with a signature error. Injected here rather
    than set globally so the requirement stays visible and local to this call.
    """
    env = os.environ.copy()
    if config.js_runtime_bin and config.js_runtime_bin.exists():
        env["PATH"] = str(config.js_runtime_bin) + os.pathsep + env.get("PATH", "")
    return env


def download_group(config: Config, rows: list[dict], label: str) -> dict:
    """Download one group in a subprocess, showing live progress from the state DB."""
    global _PROGRESS

    handle, temp_path = tempfile.mkstemp(prefix=f"{config.run_name}-", suffix=".jsonl")
    temp_manifest = Path(temp_path)
    with os.fdopen(handle, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    command = [
        sys.executable, "-m", "src.downloader",
        "--manifest", str(temp_manifest),
        "--download-root", str(config.download_root),
        "--state-db", str(config.state_db),
        "--group-name", config.run_name,
        *config.download_args(),
    ]
    if config.cookies and config.cookies.exists():
        command += ["--cookies", str(config.cookies)]

    video_ids = {row["video_id"] for row in rows}
    config.log_file.parent.mkdir(parents=True, exist_ok=True)
    with config.log_file.open("a", encoding="utf-8") as fh:
        fh.write("$ " + " ".join(command) + "\n")

    stop = threading.Event()
    with make_progress() as progress:
        _PROGRESS = progress
        task = progress.add_task(f"[cyan]DOWNLOAD  {label}", total=len(rows), note="")

        def poll() -> None:
            # The subprocess owns stdout, so progress is read from the state DB instead.
            while not stop.is_set():
                on_disk = finished_downloads(config)
                counts = summarise(state_rows(config, video_ids))
                done = max(on_disk, counts["ok"])
                note = f"[green]OK{done}[/]"
                if counts["failed"]:
                    note += f" [red]x{counts['failed']}[/]"
                progress.update(task, completed=min(len(rows), done + counts["failed"]), note=note)
                time.sleep(1.0)

        watcher = threading.Thread(target=poll, daemon=True)
        watcher.start()
        try:
            with config.log_file.open("a", encoding="utf-8") as fh:
                subprocess.run(command, cwd=ROOT, env=child_env(config),
                               stdout=fh, stderr=subprocess.STDOUT, check=False)
        finally:
            stop.set()
            watcher.join(timeout=3)
        _PROGRESS = None

    temp_manifest.unlink(missing_ok=True)

    counts = summarise(state_rows(config, video_ids))
    counts["has_files"] = finished_downloads(config) > 0
    if counts["gone"]:
        log(config, f"[dim]{label}: {counts['gone']} video(s) gone from YouTube - "
                    f"skipping them for good[/]")
    if counts["retry"]:
        log(config, f"[yellow]{label}: {counts['retry']} recoverable failure(s)[/]")
    if counts["blocked"]:
        CONSOLE.print(Panel(Text.from_markup(
            f"[bold red]YOUTUBE IS RATE-LIMITING YOU[/]  ({counts['blocked']} errors)\n\n"
            "Nothing is wrong with the videos or with this tool.\n"
            "  1. stop here (already done)\n"
            "  2. switch network - mobile hotspot, or a different connection\n"
            "  3. wait ~1 hour, then run the same command again\n\n"
            "Progress is saved. Nothing is lost."),
            border_style="red", title="blocked"))
    return counts


# --------------------------------------------------------------------- upload

def upload_group(api: HfApi, config: Config, label: str) -> int:
    """Build the split, merge metadata, upload, commit. Returns total rows in the dataset.

    Raises on failure, deliberately: the caller must not record these videos as uploaded
    unless the commit actually landed.
    """
    global _PROGRESS

    say(f"[dim]{label}: packaging audio...[/]")
    split_dir = build_split(config.group_dir)
    split_csv = split_dir / "metadata.csv"

    new_rows = read_metadata_csv(split_csv)
    if not new_rows:
        raise RuntimeError(f"{label}: nothing to upload - the split came out empty")

    # The Hub carries ONE metadata.csv for the whole dataset, so push the merged file.
    merged = merge_metadata(read_metadata_csv(config.metadata_csv), new_rows)
    write_metadata_csv(split_csv, merged)

    audio_files = sorted(f for f in (split_dir / "audio").iterdir() if f.is_file())
    size_mb = sum(f.stat().st_size for f in audio_files) / 1e6
    operations = [
        CommitOperationAdd(path_in_repo=f"train/audio/{f.name}", path_or_fileobj=str(f))
        for f in audio_files
    ]
    metadata_operation = CommitOperationAdd(
        path_in_repo="train/metadata.csv", path_or_fileobj=str(split_csv)
    )

    api.create_repo(repo_id=config.repo_id, repo_type="dataset",
                    private=config.private, exist_ok=True)

    with make_progress() as progress:
        _PROGRESS = progress
        task = progress.add_task(
            f"[magenta]UPLOAD    {label} ({size_mb:.0f} MB)", total=len(operations), note=""
        )
        # Pre-upload the LFS blobs first so the commit itself is a fast metadata call.
        # One at a time purely so the bar can move; batching would be fewer round-trips.
        for index, operation in enumerate(operations, 1):
            api.preupload_lfs_files(repo_id=config.repo_id, additions=[operation],
                                    repo_type="dataset")
            progress.update(task, completed=index)
        progress.update(task, note="[bold cyan]committing[/]")
        api.create_commit(
            repo_id=config.repo_id, repo_type="dataset",
            operations=operations + [metadata_operation],
            commit_message=f"Add {len(audio_files)} audio files ({len(merged)} total)",
        )
        _PROGRESS = None

    # ONLY now, with the commit landed, does the merged CSV become the source of truth.
    config.metadata_csv.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(split_csv, config.metadata_csv)
    return len(merged)


# --------------------------------------------------------------------- driver

def process_group(api: HfApi, config: Config, rows: list[dict], label: str,
                  gone: set[str], retry: dict) -> str:
    """One group, start to finish. Returns 'ok' | 'gone' | 'skip' | 'retry' | 'blocked'."""
    reset_group(config, [row["video_id"] for row in rows])
    counts = download_group(config, rows, label)

    gone |= counts["gone_ids"]
    for video_id in counts["retry_ids"]:
        retry[video_id] = retry.get(video_id, 0) + 1
        if retry[video_id] >= config.max_retry:
            # Tried enough times across enough runs; stop spending requests on it.
            gone.add(video_id)
            log(config, f"[dim]{label}: {video_id} failed {config.max_retry} runs - skipping[/]")
    write_json(config.gone_file, sorted(gone))
    write_json(config.retry_file, retry)

    if counts["blocked"]:
        return "blocked"

    if counts["has_files"]:
        total = upload_group(api, config, label)
        shutil.rmtree(config.group_dir, ignore_errors=True)
        extra = ""
        if counts["gone"] or counts["retry"]:
            extra = f"  [dim]({counts['gone']} gone, {counts['retry']} will retry)[/]"
        log(config, f"[bold green]OK[/]  {label}: +{counts['ok']} audio  ->  "
                    f"[bold]{total}[/] in dataset{extra}")
        return "ok"

    if counts["retry"] >= max(2, len(rows) - counts["gone"]):
        log(config, f"[yellow]{label}: whole group failed on the network - stopping.[/]")
        return "retry"
    log(config, f"[dim]{label}: 0 audio ({counts['gone']} gone, "
                f"{counts['retry']} to retry) - next group[/]")
    return "gone" if counts["gone"] and not counts["retry_ids"] else "skip"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download a YouTube manifest into a Hugging Face audio dataset.",
    )
    parser.add_argument("--config", default="config.json", help="path to config.json")
    parser.add_argument("--go", action="store_true", help="run through ALL remaining groups")
    parser.add_argument("--status", action="store_true", help="print progress and exit")
    parser.add_argument("--batch", type=int, help="videos per group (overrides config)")
    parser.add_argument("--repo-id", help="target dataset repo (overrides config)")
    parser.add_argument("--manifest", help="manifest JSONL (overrides config)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = ROOT / config_path
    config = Config.load(config_path, {
        "batch": args.batch, "repo_id": args.repo_id, "manifest": args.manifest,
    })

    rows = read_manifest(config.manifest)
    done_ids = uploaded_ids(config)
    gone = set(read_json(config.gone_file, []) or [])
    retry = dict(read_json(config.retry_file, {}) or {})

    skip = done_ids | gone | {v for v, n in retry.items() if n >= config.max_retry}
    remaining = [row for row in rows if row["video_id"] not in skip]
    # Shortest first: fast early progress, and the rare multi-hour videos go last where
    # an interruption costs the least.
    remaining.sort(key=lambda row: row.get("duration") or 0)

    if args.status:
        CONSOLE.print(Panel.fit(Text.from_markup(
            f"[bold]{config.repo_id}[/]\n"
            f"uploaded   [bold green]{len(done_ids)}[/] / {len(rows)} videos\n"
            f"skipped    {len(skip) - len(done_ids)}\n"
            f"remaining  [bold]{len(remaining):,}[/]"),
            title=config.run_name, border_style="cyan"))
        return 0

    if not remaining:
        CONSOLE.print(f"[bold green]ALL DONE[/]  {len(done_ids)}/{len(rows)}  {config.repo_id}")
        return 0

    token = os.getenv("HF_TOKEN")
    if not token:
        raise SystemExit(
            "No Hugging Face token.\n"
            '  PowerShell:  $env:HF_TOKEN = "hf_..."\n'
            '  bash:        export HF_TOKEN="hf_..."\n'
            "Create a WRITE token at https://huggingface.co/settings/tokens"
        )
    api = HfApi(token=token)
    config.download_root.mkdir(parents=True, exist_ok=True)

    awake = keep_awake(True)
    if awake:
        atexit.register(keep_awake, False)

    groups = [remaining[i:i + config.batch] for i in range(0, len(remaining), config.batch)]
    if not args.go:
        groups = groups[:1]

    has_cookies = bool(config.cookies and config.cookies.exists())
    CONSOLE.print(Panel.fit(Text.from_markup(
        f"[bold]{config.repo_id}[/]  [dim]({'private' if config.private else 'public'})[/]\n"
        f"uploaded so far: [green]{len(done_ids)}[/]/{len(rows)}   "
        f"this run: [bold]{len(groups)}[/] group(s) of {config.batch}   "
        f"cookies: {'[green]yes[/]' if has_cookies else '[yellow]no[/]'}\n"
        f"sleep: {'[green]blocked while this runs[/]' if awake else '[yellow]not blocked[/]'}"),
        title=config.run_name, border_style="cyan"))

    uploaded_now = 0
    stop_reason = None
    skip_streak = 0
    for index, group in enumerate(groups, 1):
        label = f"group {index}/{len(groups)}"
        CONSOLE.rule(f"[bold yellow]{label}[/]  [dim]{len(group)} videos   "
                     f"{len(done_ids) + uploaded_now:,}/{len(rows):,} uploaded[/]")
        try:
            status = process_group(api, config, group, label, gone, retry)
        except Exception as exc:  # noqa: BLE001 - a failed group must never kill the run silently
            CONSOLE.print(f"[bold red]{label} FAILED:[/] {exc!r}")
            stop_reason = "retry"
            break

        if status == "ok":
            uploaded_now = len(uploaded_ids(config)) - len(done_ids)
            skip_streak = 0
        elif status == "gone":
            skip_streak = 0
        elif status == "skip":
            skip_streak += 1
            if skip_streak >= 6:
                CONSOLE.print("[bold red]Many groups failing in a row - something systemic "
                              "(cookies? network? YouTube?). Stopping.[/]")
                stop_reason = "retry"
                break
        else:
            stop_reason = status
            break

    final = len(uploaded_ids(config))
    CONSOLE.print(f"\n[green]this run:[/] +{final - len(done_ids)} videos    "
                  f"[bold]{final}/{len(rows)}[/] uploaded    {len(gone)} skipped")
    if stop_reason == "blocked":
        CONSOLE.print("[bold red]Stopped - rate-limited.[/] Change network, wait ~1h, rerun.")
    elif stop_reason == "retry":
        CONSOLE.print("[bold yellow]Stopped - downloads failing.[/] Check the network, then rerun.")
    elif not args.go and len(groups) * config.batch < len(remaining):
        CONSOLE.print("continue with:  [bold]python -m src.pipeline --go[/]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
