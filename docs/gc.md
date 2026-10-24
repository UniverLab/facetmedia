# `facet gc`

`facet gc` removes what nothing uses any more and reports what it freed.
Nothing is ever collected on its own: garbage collection happens when this
verb is called, and nowhere else.

```bash
facet gc --dry-run                    # list what would go, delete nothing
facet gc                              # delete it
facet gc --dry-run --json             # the same list as data
```

It holds the [facet lock](home.md#the-lock) exclusively. One run removes:

| what                                                        | reason             |
|-------------------------------------------------------------|--------------------|
| an environment version with no `.complete` marker           | `incomplete-env`   |
| an environment version that is not the current one          | `stale-version`    |
| an environment of a package the snapshot no longer has      | `orphan-package`   |
| a `models/direct` entry no package of the snapshot lists    | `orphan-model`     |
| a registry snapshot other than the one in use               | `old-snapshot`     |
| an unfinished `<commit>.partial` snapshot                   | `partial-snapshot` |
| everything in `tmp/` (partial downloads, scratch)           | `tmp`              |
| a kept `.facet-tmp-<run-id>` directory listed by a failed run | `leftover`       |
| a `logs/<YYYY-MM-DD>` directory older than 30 days          | `old-log`          |
| uv's unused cache entries, via `uv cache prune`             | `uv-cache`         |

The **current** environment of every package in the snapshot in use is kept,
even when it looks unused: environments are persistent by default. Models are
kept unless nothing in the snapshot lists them.

A failed [`facet run`](run.md) keeps `--out/.facet-tmp-<run-id>/` for
inspection and appends its absolute path to
`<home>/state/leftovers.json`; gc removes exactly the directories that file
lists and clears it. gc never searches the disk for `.facet-tmp-*` itself, so
a directory nobody recorded is left alone.

With `FACET_REGISTRY` pointing at a checkout there is no snapshot in use, so
no snapshot under `registry/` is removed.

## `--json`

```json
{
  "items": [
    {"path": "/home/me/.facet/envs/echo/0.0.9", "bytes": 4096, "reason": "stale-version"},
    {"path": "/home/me/.facet/tmp/hf-weights.1a2b.part", "bytes": 1024, "reason": "tmp"}
  ],
  "freed_bytes": 5120,
  "dry_run": false
}
```

`--dry-run` reports the same items and deletes nothing; its `uv-cache` entry
is the size of the whole cache, while a real sweep reports what `uv cache
prune` actually freed. Without `--json` each item prints
`<path> <bytes> bytes <reason>`, then `freed N bytes` (or `would free N
bytes`).
