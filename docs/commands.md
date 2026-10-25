# Command reference

Every verb prints its human form by default and one JSON document with
`--json` (the [contract](contract.md)). `facet` and `facetmedia` are the
same command; `python -m facetmedia` works too.

The two verbs that look similar answer two different questions:

- `facet doctor` checks the **tool**: facet's own health, the package's
  tool chain, and this machine's readiness. It never downloads and never
  repairs; every failing check names a fix.
- `facet check` checks the **content**: it re-verifies one asset facet
  made against its record. It says nothing about the machine — pass it a
  capability id and it answers `invalid_input` with the hint
  `facet doctor <capability>`.

| Verb | Purpose | Lock |
|------|---------|------|
| `facet update` | download the registry at a pinned commit | exclusive |
| `facet pull` | install a tool into its own pinned environment | exclusive |
| `facet list` | list installed and available tools | none |
| `facet describe` | show a capability's full contract | none |
| `facet skill` | print the skill for facet or one package | none |
| `facet doctor` | check facet, the tool chain and this machine | shared |
| `facet check` | re-verify an asset against its record | per manifest |
| `facet run` | turn a piece into verified assets with records | per manifest |
| `facet preview` | show an asset to a person or to an agent | none |
| `facet license` | accept and list model licences | `accept` exclusive; `list` none |
| `facet rm` | remove an installed tool | exclusive |
| `facet gc` | remove unused environments and caches | exclusive |
| `facet registry` | validate a registry checkout | none |

Verbs that lock accept `--no-wait` (fail at once with `lock_busy`, exit 6)
and `--wait-timeout SECONDS` (default 3600); while waiting, facet prints
one line to stderr naming the holder. `run` and `check` follow the package
manifest's `requirements.exclusive` (the default): exclusive when set,
shared otherwise, so several light runs overlap. The asset form of `check`
always waits with the defaults.

## `facet update`

Downloads the registry at one reference, resolved to one exact commit, and
keeps that commit as an immutable snapshot — package runners are code from
the network, so the commit is pinned and never moves silently.

Flags: `--ref REF` (a branch, a tag, or a 40-hex commit; wins over
`$FACET_REGISTRY_REF`, which wins over `main`).

```console
$ facet update --ref feature/v0.1
registry 3decd7089bfbc7196d08851df7221c4ccf76d635 (ref feature/v0.1)
  ascii 0.1.0 changed
  cad 0.1.0 changed
  ffmpeg 0.1.0 changed
  kokoro 0.1.0 changed
  whisper 0.1.0 changed
```

Each package line names whether its manifest version differs from the
previous snapshot, meaning its next `pull` or `run` rebuilds the
environment. The GitHub resolve counts against the anonymous API limit;
`GITHUB_TOKEN` lifts it. Installing environments is never touched.
Lock: exclusive (`update` is the writer the other heavy verbs wait for).

Exit codes: `0` clean; `2` cannot load a registry it downloaded (deleted on
refusal); `8` the reference resolve, the tarball download, or an existing
snapshot's validation failed. On any failure the previous snapshot stays in
use.

## `facet pull`

Installs one package into `~/.facet/envs/<package>/<version>/`: a
uv-managed Python, the exact dependency set of the package's `uv.lock`,
and its models, downloaded once into `~/.facet/models/direct/` (or the
`hf/` cache) and checked by sha256. Nothing goes global.

Flags: `--verify-models` (recompute the sha256 of models already present).

```console
$ facet pull ffmpeg
ffmpeg 0.1.0 installed at /tmp/opencode/facet-docs/envs/ffmpeg/0.1.0 (no model downloads in 5.898 s)
```

Nothing is written or downloaded before the doctor's readiness checks
(compatibility, platform, RAM, disk, GPU, host libraries — plus the
package's own `doctor` verb at quick depth when it is already installed)
and the licence check pass; a model needing acceptance fails with the
exact `facet license accept <model>` command instead. An interrupted pull
is repaired by the next pull; `.complete` written last marks "installed".
Lock: exclusive.

Exit codes: `0` (`already installed` is also `0`); `2` unknown package;
`4` a model's terms are not accepted; `5` a readiness check fails; `8`
the environment build, a model download or its hash failed.

## `facet list`

Packages grouped by facet, each with its weight (`light`, `cpu-heavy`,
`gpu`).

Flags: `--available` (also list packages not installed; incompatible ones
appear only in this form), `--facet NAME` (one facet's packages).

```console
$ facet list --available
voice:
  kokoro  cpu-heavy
    kokoro.speak  audio  Narration from text on CPU (Spanish and English), checked by transcription
  whisper  cpu-heavy
    whisper.transcribe  text  Transcript, subtitles and timed words from an audio file on CPU, checked by confidence and optional reference text
audio:
  ffmpeg  light
    ffmpeg.convert  audio  Convert, resample or cut a clip
    ffmpeg.mix  audio  Lay voice layers over a music or ambience bed, with the bed ducked under the voice
    ffmpeg.normalize  audio  Bring clips to a loudness target (EBU R128, two-pass) without clipping
cad:
  cad  light
    cad.drawing  cad  2D plan from an ezdxf script: DXF with an audit, SVG and PNG views
    cad.solid  cad  3D part from a CadQuery script: STEP, STL and a line view
text:
  ascii  light
    ascii.banner  text  A text banner in the lab's figlet font (DOS Rebel by default).
    ascii.image  text  Image to ASCII art (density, stroke, braille or shape matching), checked by re-rendering.
```

Without `--available` and nothing installed, facet names the answer instead
of printing nothing: ``no packages installed; `facet list --available`
shows the registry's``. `--json` adds the machine data: per package
`name`, `version`, `facet`, `weight`, `compatible`, `installed`, sizes and
its capabilities; and the registry it read. Lock: none.

Exit codes: `0` (an empty list is a success with data, never an error).

## `facet describe`

One capability's whole contract: inputs (JSON Schema), a minimal example
piece, verification thresholds, requirements, licences, and what a pull
would still download.

Flags: `--json` carries everything; the human form is a summary.

```console
$ facet describe kokoro.speak
summary: Narration from text on CPU (Spanish and English), checked by transcription
package: kokoro 0.1.0 (Kokoro TTS (kokoro-onnx), hexgrad (model) · thewh1teagle (kokoro-onnx))
inputs: clip (array, required), language (string, default es), speed (number, default 1.0), target-lufs (number, default -16.0), voice (string, default ef_dora)
installed: no
```

`piece_example` in the `--json` data is a minimal valid piece, the
starting point for a piece file. Lock: none.

Exit codes: `0`; `2` an unknown capability id, listing the known ids in
`details.known`.

## `facet skill`

Prints the skill for facet (no argument) or one package, plus — under
`## On this machine` — what is true right now: installed or not, each
model's licence state, and, unless `--no-doctor`, the quick doctor's
checks with a fix for every failing one. This is how an agent starts.

Flags: `--reference REF` (one reference file of that package's skill; the
doctor is skipped), `--no-doctor`.

```console
$ facet skill ascii
---
name: ascii
description: ASCII art from images and figlet banners in the lab's font, verified.
---
# ASCII

Turn images into ASCII art and write banners in the lab's figlet font.
Both capabilities write the `text` kind and are checked by `verify`.

## When to use

- `ascii.banner` for CLI and README banners in the lab's style. The
  default font `dos_rebel` is the font of the UniverLab README banners.
- `ascii.image` for text renderings of images: plates, diagrams, photos
  that must live in a terminal, a README, or a commit message.
```

Skills are never installed into any agent directory; they live in the
registry snapshot and change with `facet update`. Lock: none.

Exit codes: `0`; `2` an unknown package, capability or reference.

## `facet doctor`

Questions about the tool: facet's nine own checks, then, per target, the
readiness checks and the package's `doctor` verb at two depths — `quick`
(about 10 s, writes nothing) and, by default, `full` (up to 30 s, a
throwaway output that facet removes). It reports; it never downloads or
repairs.

Flags: `--quick` (only the quick depth), plus a target: nothing (facet and
every installed package), a package name, or a capability id.

```console
$ facet doctor --quick
healthy
facet:
  ✓ python: python 3.13.12
  ✓ uv: uv 0.13.5 (x86_64-unknown-linux-gnu)
  ✓ home: /tmp/opencode/facet-docs exists, is writable, and is POSIX
  ✓ state: every state file parses
  ✓ lock: free
  ✓ registry: 3decd7089bfbc7196d08851df7221c4ccf76d635: at the pinned commit, validates
  ✓ disk: free 635.44 GB at /tmp/opencode/facet-docs
  ✓ orphans: no orphaned environments or models
  ✓ gpu: none
ascii (installed):
  ✓ compatible: requires_facet >=0.1.0b1,<0.2 satisfied by facet 0.1.0b1, protocol 1
  ✓ platform: linux in [linux]
  ✓ ram: total 19.53 GB, available 16.49 GB, need 1.0 GB
  ✓ disk: free 635.44 GB, need 0.00 GB (1.01 GB already installed)
  ✓ gpu: none
  ✓ host_libs: the package needs no host libraries
  ✓ installed: installed at /tmp/opencode/facet-docs/envs/ascii/0.1.0
  ✓ licenses: no model requires acceptance
  ✓ models: 1 model(s) present
  ✓ imports: PIL 13.0.0, numpy 2.5.4, pyfiglet 1.0.4 import
  ✓ figlet_font: dos_rebel is installed with pyfiglet
  ✓ mono_font: sometype-mono 64308 bytes, opens with Pillow
ffmpeg (installed):
  ✓ compatible: requires_facet >=0.1.0b1,<0.2 satisfied by facet 0.1.0b1, protocol 1
  ✓ platform: linux in [linux]
  ✓ ram: total 19.53 GB, available 16.44 GB, need 1.0 GB
  ✓ disk: free 635.43 GB, need 0.00 GB (1.01 GB already installed)
  ✓ gpu: none
  ✓ host_libs: the package needs no host libraries
  ✓ installed: installed at /tmp/opencode/facet-docs/envs/ffmpeg/0.1.0
  ✓ licenses: no model requires acceptance
  ✓ models: the package has no models
  ✓ binary: ffmpeg 7.0.2-static at ffmpeg-linux-x86_64-v7.0.2
  ✓ encoders: flac, libmp3lame, libopus, pcm_s16le
  ✓ imports: numpy 2.5.4, pyloudnorm 0.2.0, soundfile 0.13.1
```

Every failing line carries `→ fix: <command>`. Exit codes, in order of the
first failing class: `0` healthy; `5` `machine_cannot` (a facet check, a
readiness check, or a package doctor check); `4` `license_not_accepted`;
`7` `not_installed`; `9` `runner_failed` (a doctor verb crashed). Lock:
shared.

## `facet check`

Content only: re-verifies an asset against its record — the bytes have not
changed, and the runner's `verify` verb still passes with the same
metrics. It never judges a machine.

Flags: none; the argument is the asset or its `<asset>.facet.json` record.

```console
$ facet check /tmp/opencode/facet-docs/assets/canopy.txt
/tmp/opencode/facet-docs/assets/canopy.txt passed
$ facet check /tmp/opencode/facet-docs/assets/canopy.txt --json
{"contract": 1, "data": {"asset": "/tmp/opencode/facet-docs/assets/canopy.txt", "capability": "ascii.banner", "fresh": {"metrics": {"columns": 60, "rows": 9}, "passed": true, "thresholds": {}, "verifier": "ascii-pyfiglet"}, "passed": true, "recorded": {"metrics": {"columns": 60, "rows": 9}, "status": "passed"}}, "facet": "0.1.0b1", "ok": true}
```

The argument is the asset or its record; the printed line and `data.asset`
carry the same path, resolved. `fresh` carries the new verification next
to the `recorded` one. A record
whose asset is not installed re-installs nothing: `not_installed` (7) with
`facet pull <package>`. Lock: taken per the package manifest (shared or
exclusive), always waiting with the defaults — `check` rejects the waiting
flags.

Exit codes: `0` when the fresh verdict passes; `3` `verification_failed`
when it does not (the envelope stays `ok: true`, because the check ran);
`2` a missing file or a record whose capability is unknown; `4` a model
whose acceptance lapsed; `7` the package is not installed.

## `facet run`

The verb that makes things: `facet run <capability> <piece.toml | ->`.
It pulls the package if needed, validates the piece against the
capability's inputs, runs the tool in the package's own environment,
verifies every asset, and writes the records.

Flags: `--out DIR` (default: the piece's directory), `--no-verify`,
`--no-pull` (fail `not_installed` instead of pulling), `--timeout
SECONDS` (kill the runner's process group), `--ephemeral` (install for
this run, remove the environment after).

```console
$ facet run ffmpeg.normalize normalize.toml
take-voice.wav take-voice passed
out .
```

The piece is a TOML table with one `inputs` map — data, never code:

```toml
# normalize.toml
[[inputs.clip]]
name = "take-voice"
audio = "take-raw.wav"
```

An invalid piece fails before anything is written, listing every problem
at once:

```console
$ facet run ffmpeg.normalize empty.toml
invalid inputs for ffmpeg.normalize
$ facet run ffmpeg.normalize empty.toml --json
{"contract": 1, "error": {"code": "invalid_input", "details": {"errors": [{"field": "clip", "problem": "required"}]}, "hint": null, "message": "invalid inputs for ffmpeg.normalize"}, "facet": "0.1.0b1", "ok": false}
```

With `--json` on success, `data` carries `capability`, `out_dir`,
`pulled`, `downloaded_bytes` and one full record per asset. Failed assets
and their records are kept so an agent can inspect them. Lock: exclusive
or shared per the manifest.

Exit codes: `0` every asset passed; `3` an asset was made but failed its
check; `2` bad arguments, an invalid piece, an unknown capability, or an
asset that would overwrite a path input of the same run; `4` a model needs
acceptance; `5` the machine gate refuses; `7` `--no-pull` on an
uninstalled package; `9` the runner crashed or broke the protocol.

## `facet preview`

Shows one asset: to an agent as data, to a person through the capability's
viewer — see [preview.md](preview.md).

Flags: `--no-open` (print the viewer URL only), `--port PORT`,
`--idle-timeout SECONDS` (stop the server after idle; default 600).

```console
$ facet preview assets/canopy.txt --json
{"contract": 1, "data": {"asset": "assets/canopy.txt", "images": [], "preview": {"fields": {"columns": 60, "font": "dos_rebel", "rows": 9}, "kind": "text", "preview": 1, "summary": "DOS Rebel · 60×9"}, "record": {"name": "canopy", "kind": "text", "…": "the full record"}}, "facet": "0.1.0b1", "ok": true}
$ facet preview assets/canopy.txt --no-open
http://127.0.0.1:39651/viewer/index.html?asset=canopy.txt
```

Lock: none.

Exit codes: `0` (data, or the server started and stopped cleanly again);
`2` a missing asset, or a record whose capability is unknown; `7` the
package is not installed; `9` its `preview` verb broke the protocol.

## `facet license`

See [licenses.md](licenses.md) for the whole picture.

```console
$ facet license list
termsy-weights CC-BY-NC-4.0 not accepted
```

- `facet license accept <model-id>` — accepts in the terminal; the
  `--yes` flag accepts without asking, for an agent a person directs.
- `facet license list` — every model requiring acceptance, with status.
  `list` takes no lock and prints nothing when none does.

`accept` holds the lock exclusively. Exit codes: `0`; `2` an unknown model
(lists the known ones in `details.known`); `10` `license_needs_person`
when `accept` runs not on a terminal and without `--yes` (exit 2 when the
typed confirmation does not match — nothing is recorded).

## `facet rm`

Removes every environment of one package. The models stay — downloading
them again is the expensive part; `--models` deletes them too, but only
those no other installed package lists.

Flags: `--models`.

```console
$ facet rm ascii
/tmp/opencode/facet-docs/envs/ascii/0.1.0
freed 86478727 bytes
```

Removing an uninstalled package is a no-op with exit 0, so a script may
remove twice. Lock: exclusive.

Exit codes: `0`, always the no-op case included; `2` an unknown package
name is not an error here — a package not in the registry is a no-op too;
`6` the lock is busy and `--no-wait` was given.

## `facet gc`

Removes what nothing uses and reports what it freed. Nothing is collected
on its own; this verb is the only garbage collector.

Flags: `--dry-run` (list what would go, delete nothing). Use `--dry-run`
first: its `uv-cache` entry is the size of the whole cache, while a real
sweep reports what pruning freed.

```console
$ facet gc --dry-run
/tmp/opencode/facet-docs/cache/uv 280264681 bytes uv-cache
would free 280264681 bytes
```

It removes stale environment versions, environments of packages the
snapshot no longer has, unlisted models, old snapshots, tmp scratch, kept
leftover directories of failed runs, logs older than 30 days, and prunes
uv's cache. Environments in the active snapshot are kept. Lock:
exclusive.

Exit codes: `0`; `2` bad arguments; `6` the lock is busy and `--no-wait`
was given.

## `facet registry validate`

Validates a registry checkout or snapshot: every manifest, every runner
entry, every skill, every viewer, every model — all problems reported at
once, not only the first. Run it in a registry's CI before merging.

Flags: none; the argument is the directory.

```console
$ facet registry validate ../facetmedia-registry
$ facet registry validate ../facetmedia-registry --json
{"contract": 1, "data": {"packages": ["ascii", "cad", "ffmpeg", "kokoro", "whisper"], "path": "../facetmedia-registry", "valid": true}, "facet": "0.1.0b1", "ok": true}
```

A clean registry prints nothing and exits 0 — no news is the clean news.
Problems print to stderr as `file: key: problem`, and with `--json` the
same list is in `details.errors`. Lock: none.

Exit codes: `0` clean; `2` problems found.
