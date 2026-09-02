"""Tests for failure classification.

This is where a bug is expensive: misclassifying a rate-limit as GONE silently discards
good videos, and the loss is only visible once the dataset is finished and short. The
asymmetry is the point of most of these tests.
"""
from __future__ import annotations

import pytest

from src.classify import Outcome, classify, summarise


def test_ok_status_short_circuits():
    assert classify("ok", None) is Outcome.OK
    # A successful download with leftover warning text is still a success.
    assert classify("ok", "video unavailable") is Outcome.OK


@pytest.mark.parametrize("error", [
    "ERROR: [youtube] abc: Private video. Sign in if you've been granted access",
    "ERROR: [youtube] abc: This video is no longer available because the uploader has closed their account",
    "ERROR: [youtube] abc: This video has been removed by the uploader",
    "ERROR: [youtube] abc: Join this channel to get access to members-only content",
    "ERROR: [youtube] abc: The uploader has not made this video available in your country",
    "ERROR: [youtube] abc: This video has been removed for violating YouTube's policy",
    "ERROR: [youtube] abc: Sign in to confirm your age. This video may be inappropriate",
])
def test_permanent_failures_are_gone(error):
    assert classify("error", error) is Outcome.GONE


@pytest.mark.parametrize("error", [
    "ERROR: [youtube] abc: Sign in to confirm you're not a bot",
    "ERROR: [youtube] abc: HTTP Error 429: Too Many Requests",
    "ERROR: unable to download video data: HTTP Error 429",
    "ERROR: [youtube] abc: This content isn't available, try again later",
])
def test_rate_limiting_is_blocked(error):
    assert classify("error", error) is Outcome.BLOCKED


@pytest.mark.parametrize("error", [
    "ERROR: unable to download video data: <urlopen error [Errno 11001] getaddrinfo failed>",
    "ERROR: The read operation timed out",
    "ERROR: ('Connection aborted.', ConnectionResetError(10054, ...))",
    "ERROR: HTTP Error 503: Service Unavailable",
])
def test_transient_failures_are_retry(error):
    assert classify("error", error) is Outcome.RETRY


def test_bare_video_unavailable_is_retry_not_gone():
    """The single most important case in this module.

    YouTube returns a bare "Video unavailable" both for dead videos and when it is
    throttling the caller. Treating it as permanent would drop thousands of good videos
    over a long run. A needless retry costs seconds; a wrong GONE is unrecoverable.
    """
    assert classify("error", "ERROR: [youtube] abc: Video unavailable") is Outcome.RETRY


def test_unknown_errors_default_to_retry():
    """Never discard a video because of an error we have not seen before."""
    assert classify("error", "ERROR: something nobody has ever seen") is Outcome.RETRY
    assert classify("error", "") is Outcome.RETRY
    assert classify("error", None) is Outcome.RETRY


def test_blocked_wins_over_gone():
    """Order matters: a block that happens to contain video-ish wording is still a block.

    Reading this as GONE would discard the entire group the moment throttling started.
    """
    error = "Sign in to confirm you're not a bot. This video is not available."
    assert classify("error", error) is Outcome.BLOCKED


def test_age_gate_and_bot_gate_are_different_outcomes():
    """Both start with "Sign in to confirm" and mean opposite things."""
    assert classify("error", "Sign in to confirm your age") is Outcome.GONE
    assert classify("error", "Sign in to confirm you're not a bot") is Outcome.BLOCKED


def test_classification_is_case_insensitive():
    assert classify("error", "PRIVATE VIDEO") is Outcome.GONE
    assert classify("error", "HTTP ERROR 429") is Outcome.BLOCKED


def test_summarise_counts_and_partitions_ids():
    rows = [
        ("v1", "ok", ""),
        ("v2", "error", "Private video"),
        ("v3", "error", "The read operation timed out"),
        ("v4", "error", "Sign in to confirm you're not a bot"),
        ("v5", "error", "Video unavailable"),
    ]
    result = summarise(rows)

    assert result["ok"] == 1
    assert result["gone"] == 1
    assert result["blocked"] == 1
    assert result["retry"] == 2
    assert result["failed"] == 4
    assert result["gone_ids"] == {"v2"}
    assert result["retry_ids"] == {"v3", "v5"}


def test_summarise_of_nothing_is_all_zero():
    result = summarise([])
    assert result["ok"] == result["failed"] == 0
    assert result["gone_ids"] == set()
    assert result["retry_ids"] == set()


def test_blocked_ids_are_not_marked_gone_or_retried():
    """A blocked video is neither dead nor the video's fault - it must stay untouched.

    Putting it in gone_ids would discard it; putting it in retry_ids would burn one of
    its limited retries for something that was never about this video.
    """
    result = summarise([("v1", "error", "HTTP Error 429: Too Many Requests")])
    assert result["gone_ids"] == set()
    assert result["retry_ids"] == set()
