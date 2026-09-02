"""Tests for the AudioFolder split builder and the running metadata merge."""
from __future__ import annotations

import json

from src.audiofolder import (
    COLUMNS,
    build_split,
    find_audio,
    merge_metadata,
    read_metadata_csv,
    write_metadata_csv,
)


def make_video(group_dir, video_id, suffix=".webm", size=64, metadata=True):
    video_dir = group_dir / video_id
    video_dir.mkdir(parents=True, exist_ok=True)
    (video_dir / f"{video_id}{suffix}").write_bytes(b"x" * size)
    if metadata:
        (video_dir / "metadata.json").write_text(
            json.dumps({"source_row": {
                "video_id": video_id,
                "title": f"Title {video_id}",
                "video_url": f"https://www.youtube.com/watch?v={video_id}",
                "duration": 120,
                "channel_title": "A Channel",
                "tags": ["one", "two"],
            }}),
            encoding="utf-8",
        )
    return video_dir


def test_find_audio_ignores_partial_downloads(tmp_path):
    """A .part file is an interrupted download and must never ship as real audio."""
    video_dir = tmp_path / "v1"
    video_dir.mkdir()
    (video_dir / "v1.webm.part").write_bytes(b"half a file")
    assert find_audio(video_dir) is None

    (video_dir / "v1.webm").write_bytes(b"complete")
    assert find_audio(video_dir).name == "v1.webm"


def test_find_audio_ignores_empty_files(tmp_path):
    video_dir = tmp_path / "v1"
    video_dir.mkdir()
    (video_dir / "v1.webm").write_bytes(b"")
    assert find_audio(video_dir) is None


def test_build_split_lays_out_audiofolder(tmp_path):
    group = tmp_path / "group"
    make_video(group, "aaa")
    make_video(group, "bbb")

    split = build_split(group)

    assert (split / "audio" / "aaa.webm").exists()
    assert (split / "audio" / "bbb.webm").exists()

    rows = read_metadata_csv(split / "metadata.csv")
    assert [row["file_name"] for row in rows] == ["audio/aaa.webm", "audio/bbb.webm"]
    assert rows[0]["title"] == "Title aaa"
    assert rows[0]["channel"] == "A Channel"
    assert json.loads(rows[0]["tags"]) == ["one", "two"]


def test_file_name_is_the_first_column(tmp_path):
    """`datasets` matches on file_name; some loaders assume it is column zero."""
    assert COLUMNS[0] == "file_name"

    group = tmp_path / "group"
    make_video(group, "aaa")
    split = build_split(group)
    header = (split / "metadata.csv").read_text(encoding="utf-8-sig").splitlines()[0]
    assert header.split(",")[0] == "file_name"


def test_build_split_skips_videos_without_audio(tmp_path):
    group = tmp_path / "group"
    make_video(group, "good")
    (group / "failed").mkdir(parents=True)  # a video that never downloaded

    split = build_split(group)
    rows = read_metadata_csv(split / "metadata.csv")
    assert [row["video_id"] for row in rows] == ["good"]


def test_build_split_survives_missing_or_broken_metadata(tmp_path):
    """Audio with unreadable metadata still ships - the file is the valuable part."""
    group = tmp_path / "group"
    make_video(group, "nometa", metadata=False)
    broken = make_video(group, "broken")
    (broken / "metadata.json").write_text("{not json", encoding="utf-8")

    rows = read_metadata_csv(build_split(group) / "metadata.csv")
    ids = {row["video_id"] for row in rows}
    assert ids == {"nometa", "broken"}


def test_build_split_is_idempotent(tmp_path):
    group = tmp_path / "group"
    make_video(group, "aaa")

    first = read_metadata_csv(build_split(group) / "metadata.csv")
    second = read_metadata_csv(build_split(group) / "metadata.csv")
    assert first == second


def test_metadata_csv_roundtrips_utf8(tmp_path):
    """Video titles are rarely ASCII. A mangled title is a corrupted dataset column."""
    path = tmp_path / "metadata.csv"
    for title in ("مرحبا بالعالم", "日本語のタイトル", "Ünïcödé — em—dash"):
        write_metadata_csv(path, [{"file_name": "audio/a.webm", "title": title}])
        assert read_metadata_csv(path)[0]["title"] == title


def test_metadata_csv_has_no_bom_in_the_first_column_name(tmp_path):
    """Written with a BOM for Excel, so it must be read back with utf-8-sig."""
    path = tmp_path / "metadata.csv"
    write_metadata_csv(path, [{"file_name": "audio/a.webm"}])
    assert "file_name" in read_metadata_csv(path)[0]


def test_merge_metadata_adds_new_and_replaces_existing():
    existing = [
        {"file_name": "audio/a.webm", "title": "old A"},
        {"file_name": "audio/b.webm", "title": "B"},
    ]
    new = [
        {"file_name": "audio/a.webm", "title": "new A"},
        {"file_name": "audio/c.webm", "title": "C"},
    ]
    merged = {row["file_name"]: row["title"] for row in merge_metadata(existing, new)}
    assert merged == {"audio/a.webm": "new A", "audio/b.webm": "B", "audio/c.webm": "C"}


def test_merge_metadata_never_loses_earlier_uploads():
    """The running CSV is the record of everything committed so far.

    Dropping a row would make the pipeline re-download and re-upload a file that is
    already in the dataset.
    """
    existing = [{"file_name": f"audio/{n}.webm"} for n in range(100)]
    merged = merge_metadata(existing, [{"file_name": "audio/new.webm"}])
    assert len(merged) == 101


def test_read_metadata_csv_of_missing_file_is_empty(tmp_path):
    """First run: there is no running CSV yet, and that is not an error."""
    assert read_metadata_csv(tmp_path / "does-not-exist.csv") == []
