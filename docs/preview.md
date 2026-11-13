---
title: facet preview
description: How facet shows an asset to an agent as data and to a person through its viewer.
order: 4
---

# `facet preview`

`facet preview` shows one asset facet made: to an agent as data, to a
person through the capability's viewer. It also shows a whole folder —
every asset below it that has a record — as one gallery. Both forms speak
the same `--json` contract as everything else and take no lock.

```console
$ facet preview out/take-voice.wav --json          # the record plus the preview file
$ facet preview out/take-voice.wav                 # serve the viewer, open the browser
$ facet preview out/take-voice.wav --no-open       # serve the viewer, print the URL only
$ facet preview out/take-voice.wav --port 8901 --idle-timeout 60
$ facet preview ~/facet-verify/pieces              # the whole folder, as a gallery
```

One kind is different: for a `document` asset the person form opens the
PDF itself — see [the document exception](#person-form-facet-preview-asset).

The argument names the asset, its record, or a folder. The record's
provenance names the capability, whose package must still be installed
**only when the preview must be rebuilt**: when `<asset>.preview.json` is
missing or older than the asset, facet calls the runner's `preview` verb,
and with the package gone it fails with `not_installed` and the hint
`facet pull <package>`. A preview file that is not older than its asset is
fresh: facet shows it as it is, installed package or not, because the
finished piece already holds everything the viewer needs.

## Agent form: `facet preview <asset> --json`

Nothing is opened and no server starts. The data is
`{"asset", "record", "preview", "images"}`: the asset's path relative to
the working directory, its record, the parsed preview file, and the
preview's images as absolute paths (an empty list for an asset whose
preview lists no image files):

```json
{"asset": "assets/canopy.txt", "images": [], "preview": {"fields": {"columns": 60, "font": "dos_rebel", "rows": 9}, "kind": "text", "preview": 1, "summary": "DOS Rebel · 60×9"}, "record": {"name": "canopy", "kind": "text", "…": "the full record, as run --json reports it"}}
```

An agent reads the preview instead of the asset: a misheard line in a
narration shows up in its preview, so does a misplaced hole in a CAD part.
The per-kind fields a preview may carry are published by the registry, one
draft-2020-12 schema per asset kind:

- [`audio.schema.json`](https://github.com/UniverLab/facetmedia-registry/blob/main/schemas/preview/audio.schema.json)
- [`cad.schema.json`](https://github.com/UniverLab/facetmedia-registry/blob/main/schemas/preview/cad.schema.json)
- [`generic.schema.json`](https://github.com/UniverLab/facetmedia-registry/blob/main/schemas/preview/generic.schema.json)
  (the fallback for kinds without their own, including `text`)

A package whose preview does not match its kind's schema is a registry
review blocker — see the [schemas README](https://github.com/UniverLab/facetmedia-registry/blob/main/schemas/preview/README.md).

### Document pages

A `document` preview writes the PDF's page images, one PNG per page in
page order, as its `pages` list plus a `page_count`. The agent form rides
them on the same `images` array, at most
10 pages; past 10, the `preview` object carries `truncated: true` and the
rest of the pages never reach the agent — it still sees them through
`preview.pages`, so judging the text layout of a 300-page thesis means
asking the package for what pages matter, not facet for the whole book:

```json
{"asset": "assets/thesis.pdf", "record": {"name": "thesis", "kind": "document", "…": "the full record"},
 "preview": {"kind": "document", "preview": 1, "page_count": 11, "pages": ["page-01.png", "…", "page-11.png"], "summary": "a footnoted draft", "truncated": true},
 "images": ["/abs/out/page-01.png", "…", "/abs/out/page-10.png"]}
```

## Person form: `facet preview <asset>`

The viewer is the capability's `preview.viewer` (default `generic`) from
the registry snapshot in use, at `viewers/<viewer>/`; the shared kit lives
at `viewers/_kit/`. Facet serves them on 127.0.0.1 — a free port, or
`--port` — read-only and only three paths: `/viewer/...` from the viewer's
directory, `/kit/...` from the kit, and `/asset/<name>` for the asset, its
record, its preview file and its images — nothing else beside the asset,
and no directory listing. Any other path is 404, and so is any path with
`..`.

The viewer opens at `http://127.0.0.1:<port>/viewer/index.html?asset=<name>`,
which is always the first line facet prints to stdout; it then opens that
URL with `wslview`, `explorer.exe`, `xdg-open` or `open`, whichever fits
the platform. With `--no-open`, or when no opener works, the printed URL
is all you get:

```console
$ facet preview assets/canopy.txt --no-open
http://127.0.0.1:39651/viewer/index.html?asset=canopy.txt
```

The server runs in the foreground and stops on Ctrl+C or after
`--idle-timeout` seconds without requests (default 600). A viewer missing
from the registry falls back to `generic`; when that is missing too, facet
prints the preview JSON and exits 0. The core carries no HTML of its own —
the viewers live in the registry's
[`viewers/`](https://github.com/UniverLab/facetmedia-registry/blob/main/viewers/README.md),
one directory per kind with a `_kit/` shared between them.

### The document exception

A `document` asset needs no viewer: the person preview is the PDF itself.
Facet prints its `file://` URL — the asset's own path, not a local
server — and opens it with the same platform opener; the `pages` PNGs are
for agents, and no viewer starts for the person. `--no-open` prints the
URL only; `--port` and `--idle-timeout` do nothing for a document.

```console
$ facet preview out/thesis.pdf --no-open
file:///home/facet/out/thesis.pdf
```

## Folder form: `facet preview <folder>`

`facet preview <folder>`, or `facet preview` in the folder you are in,
collects every asset below the folder, recursively, that has a sibling
`.facet.json` record, and sorts them by their path relative to the folder.
No runner starts until a card is opened: each asset's `.preview.json` is
classified, and nothing else runs.

- `fresh` — the preview file is not older than the asset;
- `stale` — the preview file is older than the asset;
- `missing` — there is no preview file.

`--kind image|audio|video|document|text|cad` keeps one kind. A folder that
holds no assets — after the filter — is `invalid_input`, exit 2, and the
message names the folder.

### Agent form: `facet preview <folder> --json`

Nothing is opened, no server starts and no runner runs. The data is
`{"folder", "assets"}`: the folder's name and one entry per asset, in
order:

```json
{"folder": "pieces",
 "assets": [
   {"asset": "caja/caja-product.png", "bytes": 48213, "capability": "conjure.image",
    "created": "2026-11-12T10:04:11Z", "duration_s": null, "kind": "image",
    "preview": {"preview": 1, "kind": "image", "summary": "a boxed product"},
    "preview_state": "fresh", "verified": true},
   {"asset": "voices/intro.wav", "bytes": 911020, "capability": "speak.voice",
    "created": "2026-11-12T10:09:52Z", "duration_s": 4.25, "kind": "audio",
    "preview": null, "preview_state": "stale", "verified": false}
 ]}
```

`kind`, `capability`, `created`, `bytes` and `duration_s` come from the
record (`duration_s` is null when the record states none); `verified` is
true only when the record's check passed; `preview` is the parsed preview
file for a fresh asset and null otherwise.

### The gallery

A person gets one server on 127.0.0.1, serving exactly these routes. It
writes nothing but the preview a card builds on `/open/<n>`:

| route | what it serves |
| --- | --- |
| `/gallery/…` | the registry snapshot's `viewers/gallery/`; the page printed is `/gallery/index.html` |
| `/kit/…` | the shared kit, as for one asset |
| `/gallery.json` | `{"folder", "assets"}`: one card per asset, in order |
| `/thumb/<n>` | the asset itself for an image, otherwise the first image of its fresh preview, otherwise 404 |
| `/open/<n>` | makes the card the current asset and answers 302 to `/viewer/index.html?asset=<name>` — a `document` is served as the PDF itself — building the preview first when it is `stale` or `missing` and its package is installed; a failed build answers 500 with a plain-text body naming the asset and the runner's error, and leaves the card; otherwise 404 |
| `/viewer/…` | the current asset's viewer, as for one asset |
| `/asset/<name>` | the current asset's own files, as for one asset |

A card carries the `--json` fields without `preview`, plus `n` (the index
the routes use), `name` and `package` (from the record), `thumb` and
`open` (a route, or null). A fresh card always has an `open` route; a
stale or missing card has one when its package is installed — opening it
builds the preview first, through the same runner call
`facet preview <asset path>` makes, and the card turns fresh. A card
whose package is not installed has `open: null`.

Builds run one at a time: a card opened while another is still building
waits for that build, then starts its own. The route answers when the
build is done — nothing is left running in the background.

One asset is current at a time: opening a second card replaces it, so a
tab left on the earlier asset stops loading its files, and both `/viewer/`
and `/asset/` answer 404 before any `/open/<n>`.

```console
$ facet preview ~/facet-verify/pieces --no-open
http://127.0.0.1:39651/gallery/index.html
```

Off a terminal — `facet preview <folder> | less`, or a script — the cards
are printed as JSON and no server starts. When the snapshot has no
`viewers/gallery/index.html` (an older snapshot) the list is printed as a
plain table instead — off a terminal too — and facet exits 0.

`--no-open`, `--port` and `--idle-timeout` mean what they mean for one
asset: the same server, the same opener, the same idle stop.
