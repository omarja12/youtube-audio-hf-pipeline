"""Decide what a download failure means.

This is the most consequential logic in the project and the reason it is a module of its
own with tests. yt-dlp reports every failure as a string; that string decides whether a
video is dropped forever, retried later, or the whole run stops.

Three outcomes:

``GONE``     the video will never download - deleted, private, members-only, region-locked.
             Recorded permanently so later runs do not waste requests on it.
``BLOCKED``  YouTube is rate-limiting this IP. Nothing is wrong with the video. The only
             correct response is to STOP - retrying makes the block worse and marks good
             videos as failed.
``RETRY``    transient. Network blips, and anything unrecognised.

The subtle one is a bare "Video unavailable". YouTube returns that both for genuinely
dead videos AND when it is throttling you, and there is no way to tell from the string
alone. Classifying it as GONE would silently discard thousands of perfectly good videos
across a long run, and the loss is invisible until the dataset is finished and short.
So it is deliberately RETRY: the cost of a needless retry is seconds, the cost of a
wrong GONE is permanent data loss.

The same asymmetry drives the default: anything unrecognised is RETRY, never GONE.
"""
from __future__ import annotations

from enum import Enum


class Outcome(str, Enum):
    OK = "ok"
    GONE = "gone"
    BLOCKED = "blocked"
    RETRY = "retry"


# Unambiguous, video-specific, permanent. Every phrase here names a property of the
# video itself; none of them can be produced by rate limiting.
GONE_MARKERS = (
    "private video",
    "video is private",
    "been terminated",
    "account terminated",
    "no longer available",
    "members-only",
    "join this channel",
    "removed by the uploader",
    "video has been removed",
    # Matches both "not available in your country" and yt-dlp's actual wording,
    # "The uploader has not made this video available in your country".
    "available in your country",
    "who has blocked it",
    "confirm your age",
    "sign in to confirm your age",
    "violat",
)

# The IP is being throttled. Stop the run.
BLOCKED_MARKERS = (
    "not a bot",
    "sign in to confirm you're not a bot",
    "too many requests",
    "http error 429",
    "rate-limited",
    "try again later",
    "content isn't available",
)

# Explicitly transient. Listed for documentation value - anything unmatched is RETRY
# anyway - except "video unavailable", which is here to OVERRIDE the instinct to
# treat it as permanent. See the module docstring.
RETRY_MARKERS = (
    "getaddrinfo",
    "timed out",
    "connection aborted",
    "connection reset",
    "network is unreachable",
    "temporary failure in name resolution",
    "failed to establish a new connection",
    "read timed out",
    "page needs to be reloaded",
    "unable to download",
    "http error 5",
    "video unavailable",
    "this video is unavailable",
    "requested format is not available",
    "no video formats found",
)


def classify(status: str, error: str | None) -> Outcome:
    """Classify one row of the download state table.

    Order matters. BLOCKED is checked before GONE because a rate-limit message can
    contain video-ish wording, and mistaking a block for a dead video would discard the
    entire group. GONE is checked before RETRY because "sign in to confirm your age" and
    "sign in to confirm you're not a bot" are different problems that share a prefix.
    """
    if status == "ok":
        return Outcome.OK

    lowered = (error or "").lower()
    if any(marker in lowered for marker in BLOCKED_MARKERS):
        return Outcome.BLOCKED
    if any(marker in lowered for marker in GONE_MARKERS):
        return Outcome.GONE
    return Outcome.RETRY


def summarise(rows: list[tuple[str, str, str]]) -> dict:
    """Fold ``(video_id, status, error)`` rows into per-outcome counts and id sets."""
    counts = {outcome: 0 for outcome in Outcome}
    gone_ids: set[str] = set()
    retry_ids: set[str] = set()

    for video_id, status, error in rows:
        outcome = classify(status, error)
        counts[outcome] += 1
        if outcome is Outcome.GONE:
            gone_ids.add(video_id)
        elif outcome is Outcome.RETRY:
            retry_ids.add(video_id)

    return {
        "ok": counts[Outcome.OK],
        "gone": counts[Outcome.GONE],
        "blocked": counts[Outcome.BLOCKED],
        "retry": counts[Outcome.RETRY],
        "failed": counts[Outcome.GONE] + counts[Outcome.BLOCKED] + counts[Outcome.RETRY],
        "gone_ids": gone_ids,
        "retry_ids": retry_ids,
    }
