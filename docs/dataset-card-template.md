# Dataset card template

Copy this to `README.md` in your dataset repository on the Hub. The pipeline never
touches that file — it only writes `train/` — so a card you push once stays put.

A dataset with no card is a folder of files nobody can reuse: the licence, the language
and the collection method are exactly what a downstream user needs and cannot infer.

---

```markdown
---
license: other
task_categories:
  - automatic-speech-recognition
  - audio-classification
language:
  - <ISO 639-1 code, e.g. ar>
size_categories:
  - 1K<n<10K
configs:
  - config_name: default
    data_files:
      - split: train
        path: train/metadata.csv
---

# <Dataset name>

Audio collected from public YouTube videos with
[youtube-audio-hf-pipeline](https://github.com/<you>/youtube-audio-hf-pipeline).

## Contents

| | |
|---|---|
| Clips | <n> |
| Total duration | <hours> h |
| Format | Opus / WebM, as served by YouTube |
| Sample rate | source (not resampled) |

## Layout

AudioFolder. `train/metadata.csv` carries one row per file; `file_name` is the join key.

| column | meaning |
|---|---|
| `file_name` | `audio/<video_id>.<ext>` |
| `video_id` | YouTube id — the provenance key |
| `title` | video title as published |
| `url` | original watch URL |
| `duration` | seconds, from the source manifest |
| `channel` / `channel_id` | uploader |
| `view_count` | at collection time |
| `tags` | JSON array |

```python
from datasets import load_dataset

ds = load_dataset("<user>/<dataset>", split="train")
print(ds[0]["audio"]["array"].shape, ds[0]["title"])
```

## Collection

Downloaded with `yt-dlp` at `bestaudio`, no re-encoding. Videos that were private,
deleted, members-only or region-blocked at collection time are absent; the id list is
otherwise complete.

## Limitations and bias

- **Not transcribed.** Audio and metadata only.
- **No speaker labels**, no consent process — these are public uploads.
- **Skewed by whatever the source channels are.** Say which, and how they were chosen.
- **Audio quality varies** from studio to phone microphone.
- **Titles and tags are uploader-supplied**, so they are noisy and sometimes wrong.

## Licence and rights

The *metadata* in this repository is released under <licence>. The **audio is not
relicensed** — each clip remains the property of its uploader, and is included here
under <state your basis: fair dealing / research exemption / explicit permission>.

Do not redistribute the audio without establishing your own basis for doing so.

## Citation

```bibtex
@misc{<key>,
  title  = {<Dataset name>},
  author = {<You>},
  year   = {<year>},
  url    = {https://huggingface.co/datasets/<user>/<dataset>}
}
```
```
