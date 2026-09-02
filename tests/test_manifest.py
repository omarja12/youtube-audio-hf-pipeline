"""Tests for manifest reading.

Windows tooling (Notepad, PowerShell's Set-Content) writes a UTF-8 BOM. Every reader in
this project has to tolerate one, because the alternative is an opaque JSON error on
line 1 that tells the user nothing about what is actually wrong.
"""
from __future__ import annotations

import json

import pytest

from src.manifest import read_json, read_manifest, write_json, write_manifest


def write_lines(path, rows, encoding="utf-8"):
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding=encoding)
    return path


def test_reads_rows_and_derives_the_url(tmp_path):
    path = write_lines(tmp_path / "m.jsonl", [{"video_id": "abc", "title": "T"}])
    rows = read_manifest(path)
    assert rows[0]["video_id"] == "abc"
    assert rows[0]["video_url"] == "https://www.youtube.com/watch?v=abc"


def test_explicit_url_is_kept(tmp_path):
    path = write_lines(tmp_path / "m.jsonl", [{"video_id": "abc", "video_url": "https://example/x"}])
    assert read_manifest(path)[0]["video_url"] == "https://example/x"


def test_bom_does_not_break_the_first_row(tmp_path):
    path = write_lines(tmp_path / "m.jsonl", [{"video_id": "abc"}], encoding="utf-8-sig")
    assert read_manifest(path)[0]["video_id"] == "abc"


def test_blank_lines_are_skipped(tmp_path):
    path = tmp_path / "m.jsonl"
    path.write_text('\n{"video_id": "a"}\n\n{"video_id": "b"}\n\n', encoding="utf-8")
    assert [row["video_id"] for row in read_manifest(path)] == ["a", "b"]


def test_bad_json_names_the_line(tmp_path):
    """A 9,000-line manifest needs the line number, not just 'invalid JSON'."""
    path = tmp_path / "m.jsonl"
    path.write_text('{"video_id": "a"}\n{oops\n', encoding="utf-8")
    with pytest.raises(ValueError, match=r":2:"):
        read_manifest(path)


def test_row_without_video_id_is_rejected(tmp_path):
    path = write_lines(tmp_path / "m.jsonl", [{"title": "no id here"}])
    with pytest.raises(ValueError, match="video_id"):
        read_manifest(path)


def test_missing_manifest_exits_with_a_readable_message(tmp_path):
    with pytest.raises(SystemExit, match="Manifest not found"):
        read_manifest(tmp_path / "nope.jsonl")


def test_manifest_roundtrip_preserves_unicode(tmp_path):
    path = tmp_path / "m.jsonl"
    write_manifest(path, [{"video_id": "a", "title": "العربية 中文 Ünïcödé"}])
    assert read_manifest(path)[0]["title"] == "العربية 中文 Ünïcödé"


def test_json_state_roundtrip(tmp_path):
    path = tmp_path / "state" / "gone.json"
    write_json(path, ["a", "b"])
    assert read_json(path) == ["a", "b"]


def test_read_json_missing_returns_default(tmp_path):
    """First run: no state files exist yet, and that must not be an error."""
    assert read_json(tmp_path / "nope.json", []) == []
    assert read_json(tmp_path / "nope.json", {}) == {}
