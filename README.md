# youtube-audio-hf-pipeline

Resumable, crash-safe collection of YouTube audio into a Hugging Face `datasets`
AudioFolder — built for runs of several thousand videos from a single machine, on a
connection that rate-limits, where the run **will** be interrupted.

*[Français](README.fr.md) · [Engineering case study](docs/case-study.md)*

```
╭──────────────────── my-manifest ─────────────────────╮
│ your-name/your-audio-dataset  (private)              │
│ uploaded so far: 2200/9000   this run: 273 group(s)  │
│ cookies: yes                                         │
│ sleep: blocked while this runs                       │
╰──────────────────────────────────────────────────────╯
─────────────── group 1/273  25 videos  2200/9000 uploaded ────────────────
⠹ DOWNLOAD  group 1/273 ████████████████████░░░░  78%  18/25  OK18  0:00:41
OK  group 1/273: +25 audio  ->  2225 in dataset
```

## The problem

Downloading ten thousand videos is not ten thousand times harder than downloading one.
It is a different problem:

- YouTube **rate-limits** you, and when it does it lies — it returns "Video unavailable"
  for videos that are perfectly fine.
- The run **will** be interrupted. Network drops, a reboot, a closed laptop.
- Some videos are **genuinely dead** and must be remembered as dead, or every future run
  wastes requests rediscovering that.
- The machine **must not sleep**, and a machine left alone will.

Getting any one of these wrong doesn't crash the run. It silently produces a smaller
dataset than you think you have, and you find out at the end.

## How it works

The unit of work is a **group** of N videos:

```
reset ──▶ download ──▶ build AudioFolder ──▶ merge metadata ──▶ upload ──▶ commit ──▶ delete local
          (subprocess)                                                        │
                                                                              ▼
                                                        running metadata.csv := merged
```

That last step is the whole design. A video enters the running `metadata.csv` **only
after its commit lands**, which makes the CSV a truthful record of what is on the Hub.
Every interruption is then safe:

| Interrupted during | Result |
|---|---|
| download | local files discarded, nothing recorded, group redone |
| upload | commit never landed, nothing recorded, group redone |
| after commit | recorded — the group is skipped next run |

There is no state in which a video is recorded but absent. The cost of any crash is
bounded by one group.

## Quick start

```powershell
git clone https://github.com/<you>/youtube-audio-hf-pipeline
cd youtube-audio-hf-pipeline

python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

Copy-Item config.example.json config.json   # then set repo_id
$env:HF_TOKEN = "hf_..."                    # WRITE token, never committed

.\.venv\Scripts\python.exe -m src.pipeline --status   # what's left
.\.venv\Scripts\python.exe -m src.pipeline            # one group, then stop
.\.venv\Scripts\python.exe -m src.pipeline --go       # everything
```

On macOS or Linux use `.venv/bin/python` and `export HF_TOKEN=...`; everything except the
keep-awake guard is cross-platform.

The bundled [`examples/sample-manifest.jsonl`](examples/sample-manifest.jsonl) is eight
public videos, enough to watch a full cycle. Build your own from any channel:

```powershell
python .\scripts\make_manifest.py --url "https://www.youtube.com/@channel/videos" --limit 100 --out examples/mine.jsonl
```

### Getting downloads to actually work

Bulk YouTube downloading needs three things beyond `pip install`, and without any one of
them everything fails:

1. **`cookies.txt`** — a Netscape cookie file from a signed-in browser. Anonymous bulk
   access is blocked almost immediately. Export it with a "Get cookies.txt LOCALLY"
   extension. *Do not use `--cookies-from-browser` on Windows* — Chrome's app-bound
   encryption broke it.
2. **A JavaScript runtime** — YouTube serves a JS challenge that yt-dlp must execute.
   Install [Deno](https://deno.com) and point `js_runtime_bin` at its `bin` directory.
3. **`yt-dlp[default]` and `yt-dlp-ejs`** — plain `yt-dlp` installs fine and then fails
   on real traffic.

`cookies.txt` is in `.gitignore` and must stay there. It is a live session for whichever
account exported it — use a throwaway account, not your own.

## Configuration

Everything run-specific lives in `config.json` (gitignored; copy `config.example.json`):

| Key | Meaning |
|---|---|
| `repo_id` | target dataset, `user/name` |
| `manifest` | JSONL of videos to fetch |
| `batch` | videos per group — bigger is faster, and loses more when interrupted |
| `cookies` | path to `cookies.txt` |
| `js_runtime_bin` | directory containing the Deno binary |
| `download.workers` | parallel downloads (**4 is the ceiling**, see below) |
| `download.sleep_seconds` | delay after each video (**3s**) |

**On the download settings:** they are tuned against YouTube, not against your machine.
Above roughly 4 workers / 3s, YouTube starts returning fake "unavailable" for good videos
— faster settings produce *less* data. This was measured, not guessed.

## Design decisions worth explaining

**A bare "Video unavailable" is retried, never discarded.** YouTube returns that string
both for dead videos and when throttling you, and nothing distinguishes them. Treating it
as permanent would silently drop good videos across a long run. A needless retry costs
seconds; a wrong "gone" is unrecoverable. Every unrecognised error defaults to retry for
the same reason. See [`src/classify.py`](src/classify.py) — the most consequential file
here, which is why it is pure logic with [tests](tests/test_classify.py).

**Downloads run in a subprocess.** yt-dlp is the part that breaks, and it breaks in
threads. A process boundary means the orchestrator survives anything the downloader does
and can report it. It also gives a clean place to inject the JS runtime onto `PATH`.

**Rate-limiting stops the run.** It does not retry, back off, or continue. Continuing
while throttled marks good videos as failed and deepens the block. The tool prints what
to do — change network, wait an hour — and exits with progress saved.

**Windows is kept awake explicitly.** A long run is almost entirely network wait, which
Windows counts as idle and suspends after 20 minutes. `SetThreadExecutionState` blocks
that for the duration and releases on exit. A closed laptop lid still wins; the tool says
so rather than pretending otherwise.

## Layout

```
src/classify.py      failure classification — gone / blocked / retry
src/downloader.py    yt-dlp wrapper, bounded concurrency, SQLite state, circuit breaker
src/audiofolder.py   downloads -> train/audio + metadata.csv, and the running merge
src/pipeline.py      the orchestrator: grouping, resume, upload, progress UI
src/manifest.py      JSONL / JSON I/O
scripts/             manifest builder
tests/               46 tests, no network required
```

## Development

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
```

## License

MIT — see [LICENSE](LICENSE).

Downloading audio and *redistributing* it are different questions. This tool does the
first; the rights to do the second depend entirely on what you collect. Respect YouTube's
Terms of Service and the rights attached to the material you fetch.
