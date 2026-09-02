# Example manifests

A manifest is JSONL: one JSON object per line, one line per video. The only required
field is `video_id`. `video_url` is derived when missing; every other field is carried
through and becomes a column in the dataset's `metadata.csv`.

```json
{"video_id": "jNQXAC9IVRw", "title": "Me at the zoo", "duration": 19, "channel_title": "jawed"}
```

## `sample-manifest.jsonl`

Eight well-known public videos, enough to watch a full download → package → upload cycle
without waiting. Sorted shortest-first by the pipeline, so the 19-second one lands almost
immediately.

These ids are stable but not guaranteed — videos do get taken down. If one has died by
the time you run this, that is not a bug: the pipeline classifies it as `gone`, records
it, and carries on with the rest. Watching it do that is arguably the better demo.

## Building your own

```powershell
python .\scripts\make_manifest.py --url "https://www.youtube.com/@SomeChannel/videos" --limit 50 --out examples/my-manifest.jsonl
```

Works with any playlist or channel URL. It only reads metadata — no audio is downloaded.

## A note on what you collect

This tool downloads audio. Whether you may then *redistribute* that audio depends
entirely on the rights attached to it. The sample manifest exists to prove the pipeline
works end to end; it is not a suggestion to publish a dataset of commercial music.

For a real dataset, collect content you have the rights to, and record the provenance —
the manifest fields are carried into `metadata.csv` precisely so that provenance
survives into the published dataset.
