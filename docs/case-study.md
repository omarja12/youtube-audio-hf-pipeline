# Case study: collecting several thousand YouTube videos from one machine

*[Français](case-study.fr.md) · [back to README](../README.md)*

---

## The job

Build a speech dataset: take a manifest of several thousand YouTube videos, download the
audio, and publish it as a Hugging Face dataset in AudioFolder layout.

The constraints were not design choices. They were facts about the machine it had to run
on:

- One Windows laptop.
- A shared connection that YouTube rate-limits within about an hour.
- No way to leave it running unattended overnight.

Everything below follows from those three lines.

---

## 1. The failure that doesn't look like a failure

The first working version downloaded a batch, uploaded it, and moved on. It ran fine.
Then the counts stopped adding up: a run would report hundreds of videos "gone" that
were, on inspection, perfectly watchable in a browser.

YouTube returns `Video unavailable` for a deleted video. It also returns
`Video unavailable` when it is throttling you. The strings are identical.

The first version treated that as permanent and wrote those ids to a "never try again"
list. Every throttled minute was permanently deleting good videos from the job, and
nothing about the run looked wrong while it happened. The dataset would simply have come
out short, with no record of what was missing or why.

The fix is a three-way classification with a deliberate asymmetry:

```python
if any(marker in lowered for marker in BLOCKED_MARKERS):
    return Outcome.BLOCKED     # the IP, not the video — stop the run
if any(marker in lowered for marker in GONE_MARKERS):
    return Outcome.GONE        # unambiguous and video-specific — never retry
return Outcome.RETRY           # everything else, including "Video unavailable"
```

Three rules make this safe:

- **Only unambiguous phrases are `GONE`.** `Private video`, `removed by the uploader`,
  `members-only`. Every one names a property of the video that throttling cannot fake.
- **Unknown errors are `RETRY`.** A retry costs seconds. A wrong `GONE` is permanent.
- **`BLOCKED` is checked first.** A rate-limit message containing video-ish wording must
  never be read as a dead video, or one throttled moment discards a whole group.

A retry budget (3 runs) keeps genuinely broken videos from looping forever — the escape
hatch that makes "when in doubt, retry" affordable.

This is the only module with an unusual density of tests, including one named
`test_bare_video_unavailable_is_retry_not_gone`. It documents a decision a future reader
would otherwise be tempted to "fix".

> Writing the tests turned up a second instance of the same bug class: the marker
> `"not available in your country"` never matched, because yt-dlp's actual message is
> *"The uploader has not made this video available in your country"*. Every
> region-blocked video was being retried three times before being dropped. The test
> caught it in seconds; it had been silently costing requests for the entire run.

---

## 2. Making interruption cheap instead of rare

The run was going to be interrupted — it only ran during the day, in sessions, on a
connection that would eventually throttle. So the goal was never "don't stop". It was:
**stopping must cost almost nothing, and must never corrupt anything.**

The unit of work became a group of N videos, and the ordering within a group is the
entire safety argument:

```
reset ─▶ download ─▶ package ─▶ upload ─▶ commit ─▶ record ─▶ delete local
                                                     ▲
                             the ONLY place progress becomes permanent
```

A video is written to the running `metadata.csv` **after** its commit succeeds. That one
ordering choice collapses every crash into a safe case:

| Killed during | State on disk | State on the Hub | Next run |
|---|---|---|---|
| download | partial files | unchanged | redoes the group |
| upload | complete files | partial blobs, no commit | redoes the group |
| after commit | — | files committed | skips the group |

There is no interleaving that produces "recorded but missing". Uploaded-but-unrecorded is
possible and merely wasteful — the group is redone and the same files overwrite
themselves. That asymmetry is chosen: waste is recoverable, loss is not.

Groups also start with an explicit `reset` — delete the directory, delete the group's
rows from the state DB. Re-downloading a few already-fetched videos is cheap. Reasoning
about a half-populated directory left by a crash is not.

**Batch size is the tuning knob this exposes.** Small groups mean frequent safe points
and more upload round-trips; large groups mean fewer commits and more lost on
interruption. I ran small while the network was unreliable, and larger once it was
stable.

---

## 3. Faster settings produced less data

The obvious lever was parallelism. It was also a trap.

| workers / delay | observed |
|---|---|
| 8 / 0.5s | high throughput for ~10 minutes, then a wave of "unavailable" for good videos |
| 6 / 1s | same, slower to arrive |
| **4 / 3s** | **sustained for hours** |

Above the threshold YouTube doesn't refuse you — it starts lying, and those lies enter
the classifier as failures. The fast configuration finished each batch sooner and
collected less usable audio, while manufacturing exactly the ambiguous error that
section 1 is about.

The two problems are the same problem: throughput is capped by what the *remote* service
tolerates, and exceeding it degrades data quality before it degrades speed.

---

## 4. An arms race I did not expect to be in

Roughly a third of the total effort went into getting `yt-dlp` to work at all. In order:

**Cookies.** Anonymous bulk downloading is blocked almost immediately, so requests must
be authenticated. The obvious `--cookies-from-browser chrome` fails permanently on
current Windows — Chrome's app-bound encryption puts the cookie store out of reach. The
working path is a Netscape `cookies.txt` exported by a browser extension. It expires
after a few weeks and has to be re-exported.

**The JavaScript challenge.** YouTube began serving a JS challenge that must be executed
to derive a signature. yt-dlp cannot do this alone; it needs a JS runtime on `PATH`. The
fix was Deno, installed at user level, injected into the download subprocess's
environment:

```python
def child_env(config):
    env = os.environ.copy()
    if config.js_runtime_bin and config.js_runtime_bin.exists():
        env["PATH"] = str(config.js_runtime_bin) + os.pathsep + env.get("PATH", "")
    return env
```

Injecting into the child rather than the parent keeps the dependency visible and local —
and avoids touching the machine's global state, which I had already learned the hard way
(see §6).

**Version pinning.** The stable yt-dlp release did not yet handle the challenge; the fix
lived in a dev build, alongside `yt-dlp[default]` for the extra HTTP backends and
`yt-dlp-ejs` for the JS bridge. Plain `pip install yt-dlp` installs cleanly and then
fails on every real download — which is why `requirements.txt` here carries a comment
explaining why each pin exists.

**The general lesson:** when a dependency is adversarial — actively changing to prevent
what you're doing — treat its version and its environment as part of your configuration,
not as ambient facts.

---

## 5. The machine went to sleep

I came back from lunch to a laptop that had suspended itself mid-run.

Windows counts "idle" as absence of keyboard and mouse input. A download pipeline is
minutes of network wait with no input at all, so a long run looks exactly like an
unattended machine. The power plan slept it after 20 minutes.

The wrong fix is to change the user's power plan globally. The right one is for the
process to declare what it needs, for as long as it needs it:

```python
set_state(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED)
atexit.register(keep_awake, False)   # released when the run ends
```

Scoped to the process, reverted on exit, and reported in the startup panel
(`sleep: blocked while this runs`) so the operator can see the guard is real.

It does not cover a closed laptop lid — Windows honours the lid switch regardless of what
any program asks. The documentation says so plainly rather than leaving the user to
discover the gap the way I did.

---

## 6. Two mistakes worth recording

**I truncated the machine's `PATH`.** Using `setx` from a Git Bash shell to add a
directory silently cut the user `PATH` at 1024 characters, taking several toolchains with
it. `setx` is not a safe way to edit `PATH`; the registry-aware
`[Environment]::SetEnvironmentVariable` is. The deeper lesson is the one that shaped §4:
prefer scoping an environment change to a child process over mutating the machine.

**I built the wrong layout first.** I started by sharding the output across many small
dataset repositories, because that was how the input manifest was labelled. The
requirement was a single dataset; the shard labels were vestigial. A day of work went in
the bin because I read the data's structure instead of the actual requirement.

---

## Results

| | |
|---|---|
| Videos in the manifest | ~9,000 |
| Permanently unavailable | well under 1% |
| Interruptions survived | many — no data loss, no double-uploads |
| Sustained rate | ~4 videos/min at 4 workers / 3s |

---

## What I would do differently

**Reconcile against the Hub, not just local state.** "Already uploaded" is read from the
local `metadata.csv`. That file is written only after a successful commit, so it is never
*wrong* — but if it were deleted, the pipeline would redo work that is already published.
A `--reconcile` flag listing `train/audio/` on the Hub would make the system recoverable
from nothing but the dataset itself. This is the one gap I would close first.

**Log events, not console output.** The run log is megabytes of raw yt-dlp progress bars
— nearly unreadable, and useless for analysis. One JSON object per group (counts,
duration, bytes, outcome) would have made the throughput table in §3 a query instead of
an afternoon of observation.

**Test the classifier from day one.** It existed for a week before it had tests, and that
week is exactly when the "Video unavailable" bug was live. The tests took under an hour
to write and immediately found a second bug of the same kind.

## What I would keep

Single-file orchestration with a subprocess boundary. It is not fashionable, but the
failure modes are all visible from one screen of code, and the process boundary earned
its place many times over. A framework here would have added abstraction without adding a
single guarantee that the ordering in §2 does not already provide.
