# Footage Pipeline — backup stage

The first slice of a macOS footage pipeline. This slice does one job and does it
carefully: **mirror a card or folder to a backup drive, verify every byte with
xxHash64, and write a manifest saying exactly what happened.**

Nothing here talks to DaVinci Resolve, any external API, or a database. Later
stages build on this skeleton.

> This repository also contains **LocalLedger**, an unrelated project — see
> [`README-localledger.md`](README-localledger.md).

## Install

Python 3.11+. From the repo root:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"      # fastapi, uvicorn, xxhash (+ pytest for tests)
```

Dependencies are pinned in `pyproject.toml` and deliberately minimal.

## Run

```bash
footage-pipeline               # or: python -m footage_pipeline.web.app
```

Then open <http://127.0.0.1:8765>.

1. **Set the destination** once — the "backup root". It's remembered in a local
   JSON settings file.
2. **Choose a source** folder for this run. The last one is pre-filled, but the
   source is always picked fresh.
3. **Start backup.** Progress updates live; a report card lands at the end with
   the counts, PASS/FAIL, and the manifest path.

Both pickers open the real macOS "choose folder" dialog via `osascript`, because
a browser's `<input webkitdirectory>` never exposes an absolute filesystem path
and this app needs the real one. On non-macOS the picker returns HTTP 501 and the
UI falls back to typing a path — handy for development, and the engine itself is
fully cross-platform.

## What the backup guarantees

**Straight mirror.** A file's destination is the backup root joined with its path
*relative to the chosen source root*. Nothing is flattened, renamed, or filtered
— all files and subfolders, including dotfiles and sidecars, byte for byte.

**Nothing is destroyed.** The source is only ever opened for reading. Existing
destination files are never overwritten or deleted: new files are created with an
exclusive `open(..., "xb")`, which fails rather than clobbering.

**Every file is verified.** The source is hashed while it is copied (one pass,
streamed in 4 MB chunks so a 200 GB card never lands in RAM), then the
destination is read back and hashed, and the two must match. Modification times
are preserved (`copystat`).

**Decisions come from hashes, not bookkeeping.** For each file:

| Destination | Result |
|---|---|
| absent | **COPIED** — copy, then verify |
| present, hash matches | **SKIPPED** — already backed up |
| present, hash differs | **CONFLICT** — left completely untouched, recorded, run continues |
| error (I/O, permissions, disconnect) | **FAILED** — recorded, run continues |

Manifests are output only. The engine never reads a previous one to decide
whether to copy — delete a file at the destination and the next run copies it
again, whatever an old manifest claims.

**Symlinks are skipped, never followed** (no loops), and listed in the report.

**Pre-flight.** Before copying, the run sizes the work and checks free space at
the destination. If there isn't room it refuses to start and copies nothing.

**A run ends FAIL** if there is any conflict or any failure. Re-running resumes
naturally — everything already verified is skipped.

## Manifests

One per run, under `<backup_root>/_backup_manifests/<timestamp>/`:

- `manifest.json` — run metadata (timestamps, source, destination, totals) and
  one row per file: relative path, size, source hash, destination hash, status,
  and any error.
- `run.log` — the same run in human-readable form.

## Layout

```
src/footage_pipeline/
  config.py            persisted settings (local JSON — no database)
  logging_setup.py     shared logging + per-run log files
  backup/core.py       UI-agnostic copy + verify + manifest engine
  backup/manifest.py   manifest read/write
  web/app.py           FastAPI endpoints + native folder picker
  web/static/          frontend (plain HTML/CSS/JS)
scripts/make_test_tree.py   generates a nested dummy tree for testing
tests/test_backup_core.py   scenario tests T1-T5
```

`backup/core.py` imports nothing from the web layer and knows nothing about
HTTP. It takes two paths and an optional progress callback and returns a result
object — later pipeline stages reuse it directly, and the web layer is a thin
caller with no copy logic of its own.

Settings live at `~/Library/Application Support/FootagePipeline/settings.json`.
Set `FOOTAGE_PIPELINE_SETTINGS` to point somewhere else.

## Tests

```bash
python scripts/make_test_tree.py /tmp/footage_src     # optional: eyeball a tree
pytest
```

The suite builds its own tree (nested folders, dotfiles, sidecars, spaces in
names, an empty file, a ~50 MB file, and symlinks) and covers:

- **T1** fresh backup into an empty destination → all COPIED and verified, PASS
- **T2** immediate re-run → all SKIPPED, zero copies, PASS
- **T3** a tampered destination file → CONFLICT, destination untouched, rest
  SKIPPED, FAIL
- **T4** a write failure → recorded, other files still processed, FAIL
- **T5** insufficient free space → refuses to start

`FP_TEST_BIG_MB=200 pytest` makes the large file bigger.

One test (`test_t4b_...`) blocks writes with a read-only folder and is skipped
when running as root, since root ignores directory permissions. The main T4 test
blocks writes a way that works for any user.

## Not in this slice

No Resolve integration, no external APIs, no database, and no delete/move/rename
of anything — by design.
