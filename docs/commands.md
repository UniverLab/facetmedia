---
title: Command reference
description: Every facet verb, its flags and its human and --json output.
order: 2
---

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
| `facet pull` | install a facet or a package, choosing what this machine can run | exclusive |
| `facet list` | list installed and available tools | none |
| `facet describe` | show a capability's full contract | none |
| `facet skill` | print the skill for facet or one package | none |
| `facet config` | store where a host application lives | none |
| `facet doctor` | check facet, the tool chain and this machine | shared |
| `facet check` | re-verify an asset against its record | per manifest |
| `facet run` | turn a piece into verified assets with records | per manifest |
| `facet example` | copy a capability's bundled example into a folder | none, per manifest with `--run` |
| `facet preview` | show an asset to a person or to an agent | none |
| `facet license` | accept and list model licences | `accept` exclusive; `list` none |
| `facet remove` | remove an installed tool | exclusive |
| `facet clean` | remove unused environments, server checkouts and caches | exclusive |
| `facet registry` | validate a registry checkout | none |

Verbs that lock accept `--no-wait` (fail at once with `lock_busy`, exit 6)
and `--wait-timeout SECONDS` (default 3600); while waiting, facet prints
one line to stderr naming the holder — except `pull` and `update`, which
keep that line for their run log and print it with `-v`/`--verbose`.
`run` and `check` follow the package manifest's
`requirements.exclusive` (the default): exclusive when set,
shared otherwise, so several light runs overlap. The asset form of `check`
always waits with the defaults.

## Help

`facet --help` lists the everyday verbs under three headings — `start`,
`make and check`, `maintain` — and closes with one line for the maintainer
verb: `maintainers: facet registry validate <registry-dir>`.

Every verb's help ends with an `examples:` section of real command lines,
and every positional argument is named for what it takes: `facet pull
<facet-or-package>`, `facet run <capability> <piece.toml>`, `facet license
accept <model-id>`. Each positional's help line says where its values come
from. A group verb's own help (`facet license --help`) carries the examples
for its sub-verbs too.

A group verb with no sub-verb (`facet license`, `facet config`, `facet
registry`) prints that group's help and exits 2. With `--json` it prints
the usual failure envelope, code `invalid_input`, instead.

## `facet update`

Downloads the registry at one reference, resolved to one exact commit, and
keeps that commit as an immutable snapshot — package runners are code from
the network, so the commit is pinned and never moves silently.

Flags: `--ref REF` (a branch, a tag, or a 40-hex commit; wins over
`$FACET_REGISTRY_REF`, which wins over `main`), and `-v`/`--verbose`, which
echoes the detailed working log on stderr — the first-use registry fetch and
the lock wait. Without it, a successful update says nothing on stderr and
that log goes to the run log only.

```console
$ facet update --ref feature/v0.1
registry 3decd7089bfbc7196d08851df7221c4ccf76d635 (ref feature/v0.1)
  ascii 0.1.0 changed
  cad 0.1.0 changed
  ffmpeg 0.1.0 changed
  kokoro 0.1.0 changed
  whisper 0.1.0 changed
```

Each package line names whether its version differs from the previous
snapshot **or** its files changed under the same version, meaning its next
`pull` or `run` rebuilds the environment: a registry fix that keeps the
version and the lock still reaches an installed package. The GitHub
resolve counts against the anonymous API limit; `GITHUB_TOKEN` lifts it.
Installing environments is never touched.
Lock: exclusive (`update` is the writer the other heavy verbs wait for).

Exit codes: `0` clean; `2` cannot load a registry it downloaded (deleted on
refusal); `8` the reference resolve, the tarball download, or an existing
snapshot's validation failed. On any failure the previous snapshot stays in
use. When a downloaded snapshot does not validate, the failure's hint always
names the attempted ref, where it came from (`--ref`, `$FACET_REGISTRY_REF`, or
the builtin `main` default), the validation problem (for example a missing
`index.toml`), and what to try instead.

## `facet pull`

Installs a facet, choosing the packages of it that this machine can run.
Name one or more facets or packages; a name that is both (today `cad`)
means the facet.

```console
$ facet pull image
rembg 0.1.0 0.52 GB install: fits this machine
comfy-xl 0.1.0 8.40 GB install: fits this machine
install 2 packages? [y/N] y
installed rembg 0.1.0
installed comfy-xl 0.1.0
0 bytes downloaded
```

Each package gets one line with its include or skip reason, decided in this
order: the platform, `--cpu` against a GPU package, a missing NVIDIA GPU,
declared VRAM and RAM, a missing host application (with the exact `facet
config set <key> <path>` fix), and finally its tier group. Packages sharing
a `tier_group` are alternatives of one tool; `--tier NAME` prefers that
tier, even when it does not fit, and warns on the line when it does not.
Without `--tier`, facet picks the member with the highest declared VRAM
that fits, or the lowest one, skipped with its reason.

A plan that installs more than one package is shown and confirmed with `y`
at a terminal; any other answer installs nothing and exits 0. `--yes`
skips the question. Without a terminal, or with `--json`, such a plan
fails with `confirmation_required` (exit 11) unless `--yes` or `--plan` is
given. A single package installs as before, with no question: naming it
positionally or with `--package` keeps today's output and the same `--json`
keys (`package`, `version`, `env`, `python`, `already_installed`,
`downloaded_bytes`, `seconds`, `models`), and `--yes` changes nothing
there.

With no facet and no `--package` at a terminal, `facet pull` opens a
wizard instead of failing: it prints facet's banner, the detected machine
(GPU and VRAM, RAM, free disk under the facet home, and each host app),
and every facet with its package count and this machine's plan for it.
Facets with at least one installable package start selected; already
installed packages are marked. Type facet numbers to toggle them, `a` for
all, Enter to accept, or `q` to quit without installing. Accepting shows
the combined plan, asks once, and installs with the same progress and
summary as `facet pull <facet>`. Without a terminal, or with `--json`, the
bare pull fails with `invalid_input` (exit 2), naming the facets and the
two forms `facet pull <facet>` and `facet pull --package <name>`.

Flags: `--package NAME` (repeatable, installs that package explicitly and
cannot be mixed with positional names), `--tier NAME`, `--cpu`, `--yes`,
`--plan` (print the plan and install nothing; with `--json` it returns
`data.plan`), `--verify-models` (recompute the sha256 of models already
present), and `-v`/`--verbose`.

At a terminal, each package that installs shows one progress line per
phase, in this order: `environment` (the uv build, the server and source
checkouts, the node runtime — a spinner with the elapsed time that becomes
a bar once the download declares its length), `models` (bytes downloaded
out of the declared bytes it still has to fetch, a bar with the percentage
and MB/s) and `artifacts` (the browser, FFmpeg, binary and npm tarballs, a
bar once a size is known and a spinner before). A phase with nothing to do
still closes its line, so an already installed package shows three `✓`
lines and no download. A host-application package downloads nothing and
draws no phase line. Each line ends with `✓` and the elapsed time when
the phase completes, or `✗` and the one-line error when it fails. Without
a terminal — a pipe or a CI log — stderr gets one plain `start phase` /
`end ✓ phase (X.Xs)` line per phase and no carriage-return animation.
`--json` draws no progress line at all; its envelope is unchanged. The
detailed working log, the plan and uv's resolution included, still goes to
the run log under `logs/<YYYY-MM-DD>/<run-id>.log`; on stderr it appears
only with `-v`/`--verbose`, which replaces the progress lines. When a pull
fails, its last 20 log lines and the `log: <path>` line are printed to
stderr even without `--verbose`.

When the packages to install need more disk than is free under the facet
home, the pull fails before any download with `machine_cannot` (exit 5)
and details naming the needed and free GB and each package's size. The
`--json` result carries `data.plan`, `data.installed`, `data.skipped` and
`data.downloaded_bytes`; when one package fails, the pull stops there and
the summary lists what was installed before the failure.

Each package installs into `~/.facet/envs/<package>/<version>/`: a
uv-managed Python, the exact dependency set of the package's `uv.lock`,
and its models, downloaded once into `~/.facet/models/direct/` (or the
`hf/` cache) and checked by sha256. Nothing goes global.

```console
$ facet pull --package ffmpeg
ffmpeg 0.1.0 installed at /tmp/opencode/facet-docs/envs/ffmpeg/0.1.0 (no model downloads in 5.898 s)
```

When a package's manifest declares `uses`, its shared modules are copied
from the registry's `lib/` into the environment as `facet_lib/<name>.py`
in the same step that installs `facet_runner_sdk`. The environment records
their fingerprint in a `.registry` marker, and a pull whose lock hash,
marker and `.package` fingerprint all still match is `already installed`;
a change in any of them rebuilds the environment. A package without `uses`
writes no `.registry` and keeps the lock-only behaviour.

The `python-env` environment also carries a `.package` marker: a sha256
over the sorted relative paths and the bytes of every file under the
package directory (`__pycache__/` and `*.pyc` excluded). It covers the
whole package — the runner, helper modules, flows, scenes and skill files —
so a registry edit that keeps the version, the lock and the entry file
still makes the environment stale, and the next pull rebuilds it from
scratch.

A package that declares `[browser]` also gets the shared Chromium build:
`facet pull` downloads the Chrome for Testing archive once from
`https://cdn.playwright.dev/builds/cft/<version>/linux64/chrome-linux64.zip`,
verifies its sha256, and extracts it into the shared browser directory
`~/.facet/browsers/chromium-<revision>/`, which every package declaring the
same channel and revision shares. The executable lands at
`chromium-<revision>/chrome-linux64/chrome`. The runner's environment carries
`PLAYWRIGHT_BROWSERS_PATH` pointing at `~/.facet/browsers`, so the
package's own `playwright` finds the browser without downloading it, and
`FACET_BROWSER` with the absolute path of that executable, so drivers that
take an explicit `executablePath` need no host Chrome. Browser builds are
only available on Linux x86-64; on any other host `pull` of a package
declaring `[browser]` refuses with `machine_cannot` (exit 5).

A package that declares `[engine.server]` also gets its pinned checkout at
`~/.facet/servers/<package>/<version>/<shortrev>/` with its own venv, and
`installed` then means both the client environment and that checkout.

A package that declares `[engine.source]` gets a pinned checkout at the same
path, a shallow branchless fetch of exactly its `rev` with no server venv;
`installed` then means both the client environment and that checkout. The
plan names it as `source <repo> @ <shortrev>`.

A package that declares `[engine.node]` instead installs a pinned Node
runtime under `~/.facet/runtimes/node/<version>/` and its npm package
tarballs under `~/.facet/node_pkgs/<name>/<version>/`, each verified by
hash, with no Python environment built. It also stages the package
directory at `~/.facet/envs/<package>/<version>/package/` — the whole
directory, so the entry's helper files come with it — and writes
`.complete`, holding the sha256 of the entry file's bytes, then `.package`
with the whole-directory fingerprint; `already installed` also means that
staged copy is current, and a pull whose `.complete` or `.package` no
longer matches restages the tree from scratch, so an edited skill or helper
file at the same version comes with it. A package
that declares `[engine.binary]` downloads its pinned zip once into
`~/.facet/bin/<name>/<version>/`, verified by sha256, with the executable
bit set on the declared `bin`.

Nothing is written or downloaded before the doctor's readiness checks
(compatibility, platform, RAM, disk, GPU, host libraries — plus the
package's own `doctor` verb at quick depth when it is already installed)
and the licence check pass; a model needing acceptance fails with the
exact `facet license accept <model>` command instead. A backend-owned
model (`backend = true`, served by the backend, never fetched) needs no
download and is reported as `backend-owned`, but its terms still gate the
run. An interrupted pull
is repaired by the next pull; the markers written last —
`.complete` marking "installed" and `.package` recording the files it was
built from — are what a later pull compares against.
Lock: exclusive.

Exit codes: `0` (`already installed` is also `0`, and a declined
confirmation installs nothing with `0`); `2` a name neither a facet nor a
package carries, mixing positional names with `--package`, or naming
nothing; `4` a model's terms are not accepted; `5` a readiness check
fails, or the packages to install need more disk than is free under the
facet home; `8` the environment build, a model download or its hash
failed; `11` a multi-package plan without a terminal to confirm it and no
`--yes` or `--plan`.

## `facet list`

Packages grouped by facet. Each package line carries its name, its weight
(`light`, `cpu-heavy`, `gpu`) and its version in the current snapshot; an
installed package adds `installed <version>` and, when the snapshot's
version is newer, `update: <installed> → <snapshot>`.

Flags: `--available` (also list packages not installed; incompatible ones
appear only in this form), `--facet NAME` (one facet's packages).

```console
$ facet list --available
voice:
  kokoro  cpu-heavy  0.1.0
    kokoro.speak  audio  Narration from text on CPU (Spanish and English), checked by transcription
  whisper  cpu-heavy  0.1.0
    whisper.transcribe  text  Transcript, subtitles and timed words from an audio file on CPU, checked by confidence and optional reference text
audio:
  ffmpeg  light  0.1.0
    ffmpeg.convert  audio  Convert, resample or cut a clip
    ffmpeg.mix  audio  Lay voice layers over a music or ambience bed, with the bed ducked under the voice
    ffmpeg.normalize  audio  Bring clips to a loudness target (EBU R128, two-pass) without clipping
cad:
  cad  light  0.1.0
    cad.drawing  cad  2D plan from an ezdxf script: DXF with an audit, SVG and PNG views
    cad.solid  cad  3D part from a CadQuery script: STEP, STL and a line view
text:
  ascii  light  0.1.0
    ascii.banner  text  A text banner in the lab's figlet font (DOS Rebel by default).
    ascii.image  text  Image to ASCII art (density, stroke, braille or shape matching), checked by re-rendering.
```

Without `--available` and nothing installed, facet names the answer instead
of printing nothing: ``no packages installed; `facet list --available`
shows the registry's``. `--json` adds the machine data: per package
`name`, `version`, `facet`, `weight`, `vram_gb`, `compatible`, `installed`,
`installed_version` (string or null), `update_available`,
sizes and its capabilities; an `available` summary of the registry's package,
capability and facet counts; and the registry it read. Lock: none.

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
`details.known`; a package name lists its capability ids in
`details.capabilities`.

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

When no registry can be loaded, `facet skill` still prints the core skill
plus a `## Packages in this registry` section with one line of advice, and
exits `0`:

- A snapshot that fails validation prints a diagnosis naming the commit and
  the problem, states that re-running update with the same ref fails
  identically, and points at a different `--ref` or `$FACET_REGISTRY_REF`.
- Any other load error (a download failure, an empty home on first use, a
  corrupt state) prints `Run \`facet update\` (will try ref '<ref>'), then
  \`facet skill\` again.`, naming the default ref it will try.

Exit codes: `0`; `2` an unknown package, capability or reference.

## `facet config`

Stores where a host application lives. facet never installs Blender or
FreeCAD: a host-app package names a dotted key (`blender.path`) and
`<home>/config.toml` holds the binary's path. The known keys are exactly
the `config_key` values the active registry's host-app packages declare, so
an unknown key is `invalid_input` with the known keys in `details.known`.
The file is written atomically, and stdout carries only the JSON envelope.

- `facet config set <key> <value>` — store one path.
- `facet config get <key>` — one value, plus whether the file carries it.
- `facet config list` — every known key, its application and its value.

Resolution tries the config key, then the package's environment variable
(`BLENDER_EXE` / `FREECAD_EXE`), then `PATH`.

```console
$ facet config set blender.path "/mnt/c/Program Files/Blender Foundation/Blender 4.2/blender.exe" --json
{"contract": 1, "data": {"key": "blender.path", "path": "/home/me/.facet/config.toml", "value": "/mnt/c/Program Files/Blender Foundation/Blender 4.2/blender.exe"}, "facet": "0.1.0b1", "ok": true}
$ facet config list --json
{"contract": 1, "data": {"keys": [{"app": "blender", "key": "blender.path", "set": true, "value": "/mnt/c/Program Files/Blender Foundation/Blender 4.2/blender.exe"}], "path": "/home/me/.facet/config.toml"}, "facet": "0.1.0b1", "ok": true}
```

Lock: none.

Exit codes: `0`; `2` an unknown key, an empty value, or a config file that
is not valid TOML.

## `facet doctor`

Questions about the tool: facet's nine own checks, then, per target, the
readiness checks and the package's `doctor` verb at two depths — `quick`
(about 10 s, writes nothing), which runs by default, and `full` via `--full`
(up to 30 s, a throwaway output that facet removes; it also starts each
package's server and runs it once). It reports; it never downloads or
repairs.

Flags: `--full` (the full depth), `--quick` (accepted; the default; both
flags together are `invalid_input`, exit 2), plus a target: nothing (facet
and every installed package), a package name, or a capability id. The
readiness list ends with `shared_modules`, which reports a stale `facet_lib`
copy with the fix `facet pull <package>`; just before it, `browser` reports a
declared Chromium extraction whose `.facet-verified` marker does not hold the
declared sha256 or whose executable is missing, with the same fix, and
`ffmpeg` reports a declared FFmpeg extraction whose `.facet-verified` marker
does not hold the declared sha256 or whose `bin/ffmpeg` or `bin/ffprobe` is
missing, with the same fix. A package that declares `[engine.server]` reports a missing or
unverified checkout under `installed`, also with the fix `facet pull
<package>`; the server is probed, never started. A package that declares
`[engine.source]` reports its missing or unverified checkout the same way,
as the `source` check. A package that declares
`[engine.node]` reports a missing or unverified extraction, or a staged
runner that is not current, under `installed` with the same fix; a
verified runtime the host cannot execute is not pull-fixable. An installed
`python-env` or `node` package whose files changed in the registry since
the pull reports under `installed` as `the package's files changed in the
registry since it was installed`, with the fix `facet pull <package>`.
None of these gates a run by itself. The `gpu` check asks `nvidia-smi`: the
binary on `PATH`, or, on WSL2, the driver's own
`/usr/lib/wsl/lib/nvidia-smi`
when `PATH` misses it, so a host whose driver keeps the binary off `PATH`
still reports the GPU's name, total and used VRAM instead of `none`, and a
package that requires a GPU no longer refuses for want of one it has. A
backend-owned model (`backend = true`,
served by the backend,
never fetched) needs no download: the `models` check stays ok and notes
`backend-owned: <id>`, while a missing acceptance still fails the
`licenses` check with `license_not_accepted` (exit 4).

```console
$ facet doctor
healthy
depth: quick
--full also starts each package's server and runs it once
facet:
  ✓ python: python 3.13.12
  ✓ uv: uv 0.13.5 (x86_64-unknown-linux-gnu)
  ✓ home: /tmp/opencode/facet-docs exists, is writable, and is POSIX
  ✓ state: every state file parses
  ✓ lock: free
  ✓ registry: 3decd7089bfbc7196d08851df7221c4ccf76d635: at the pinned commit, validates
  ✓ disk: free 635.44 GB at /tmp/opencode/facet-docs
  ✓ orphans: no orphaned environments, servers or models
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
  ✓ browser: no browser declared
  ✓ ffmpeg: no ffmpeg declared
  ✓ shared_modules: no shared modules declared
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
  ✓ browser: no browser declared
  ✓ ffmpeg: no ffmpeg declared
  ✓ shared_modules: no shared modules declared
  ✓ binary: ffmpeg 7.0.2-static at ffmpeg-linux-x86_64-v7.0.2
  ✓ encoders: flac, libmp3lame, libopus, pcm_s16le
  ✓ imports: numpy 2.5.4, pyloudnorm 0.2.0, soundfile 0.13.1
```

Every failing line carries `→ fix: <command>`. Exit codes, in order of the
first failing class: `0` healthy; `5` `machine_cannot` (a facet check, a
readiness check, or a package doctor check); `4` `license_not_accepted`;
`7` `not_installed`; `9` `runner_failed` (a doctor verb crashed). With
`--json`, `facet doctor` exits nonzero if and only if its envelope reports
`ok:false`; an unhealthy envelope keeps `data` (with `data.healthy:false`)
and carries no `error`. Lock: shared.

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
`facet pull <package>`. The package's server starts for verify only when
the asset's capability declares `verify_uses_server = true` in the
manifest (default `false`); otherwise the runner gets no
`FACET_SERVER_URL`, the same environment as a quick doctor. A package whose files changed in the registry since
the pull is refused the same way, with
`<package> <version> changed in the registry since it was installed` —
`check` never verifies with stale code. Lock: taken per the package manifest
(shared or exclusive), always waiting with the defaults — `check` rejects
the waiting flags.

Exit codes: `0` when the fresh verdict passes; `3` `verification_failed`
when it does not (the envelope stays `ok: true`, because the check ran);
`2` a missing file or a record whose capability is unknown; `4` a model
whose acceptance lapsed; `7` the package is not installed, or its files
changed in the registry since the pull.

## `facet run`

The verb that makes things: `facet run <capability> <piece.toml | ->`.
A piece is a TOML file such as `facet example <capability> <folder>`
copies and prints the command for, or hand-written; `-` reads the piece
as JSON on stdin instead. It pulls the package if needed, validates the
piece against the capability's inputs, runs the tool in the package's own
environment, verifies every asset, and writes the records.

Flags: `--out DIR` (default: the piece's directory), `--no-verify`,
`--no-pull` (fail `not_installed` instead of pulling an absent or changed
package), `--timeout SECONDS` (kill the runner's process group),
`--ephemeral` (install for this run, remove the environment after), and
`-v`/`--verbose`.

An install this run does itself follows the `facet pull` rules: one
progress line per phase at a terminal, one plain `start`/`end` line per
phase without one, nothing extra on stderr with `--json`, and the
detailed transcript — the plan and uv's resolution — on stderr only with
`-v`/`--verbose`. The transcript always goes to the run log.

An installed package whose files changed in the registry since the pull is
rebuilt before the run, exactly as an absent one; with `--no-pull` it fails
with `<package> <version> changed in the registry since it was installed`
and the hint `run facet pull <package>, or drop --no-pull`.

For a package that declares `[engine.server]`, the run probes the tool's
health endpoint and, when nothing answers, starts it on a free port, points
the client at it with `FACET_SERVER_URL`, and stops its whole process group
when the run ends — a server lives for one run, never as a daemon.

For a package that declares `[engine.source]`, the runner's environment
carries `FACET_SOURCE_DIR` with the path of its pinned checkout; the runner
puts it on `sys.path` itself.

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
clip: required
$ facet run ffmpeg.normalize empty.toml --json
{"contract": 1, "error": {"code": "invalid_input", "details": {"errors": [{"field": "clip", "problem": "required"}]}, "hint": "clip: required", "message": "invalid inputs for ffmpeg.normalize"}, "facet": "0.1.0b1", "ok": false}
```

Each entry in `details.errors` carries `field` and `problem`; a `pattern`
failure also carries the field's manifest `description` as `rule` when it is
non-empty, and the hint repeats one `<field>: <rule>` (or `<field>:
<problem>`) line per error.

With `--json` on success, `data` carries `capability`, `out_dir`,
`pulled`, `downloaded_bytes` and one full record per asset. Failed assets
and their records are kept so an agent can inspect them. Lock: exclusive
or shared per the manifest.

Exit codes: `0` every asset passed; `3` an asset was made but failed its
check; `2` bad arguments, an invalid piece, an unknown capability, or an
asset that would overwrite a path input of the same run; `4` a model the
piece selects needs acceptance — only the gated models the piece actually
selects are required (see [licenses.md](licenses.md)); `5` the machine
gate refuses; `7` `--no-pull` on an
absent or changed package; `9` the runner crashed or broke the protocol.

## `facet example`

Copies a capability's bundled example — a working piece the package ships
under `examples/` in the registry snapshot — into a folder, with every
file and folder its inputs reference, so a person has something to run
and preview. The destination defaults to `./<capability with dots as
dashes>`; an existing non-empty destination fails instead of mixing.

```console
$ facet example kokoro.speak
laboratorio.toml
facet run kokoro.speak /home/you/work/kokoro-speak/laboratorio.toml --out /home/you/work/kokoro-speak/out
```

The destination prints resolved to an absolute path, so the next command
runs as-is from anywhere.

The printed files are what was copied, then the exact next command.
`--list` prints every example of the capability instead of copying, and
`--name NAME` copies the example with that file stem. With `--run` facet
also runs that next command — through the same code path as `facet run`,
which installs a missing package — and with a terminal it then previews
the first asset exactly as `facet preview` shows it. `--out DIR` picks
the asset directory for `--run` (default `<dest>/out`); `--no-open`
passes through to the preview.

When `--run` does not open the preview — no terminal, `--no-open`, or
`--json` — the text output ends one line after `out <dir>`, naming the
path `facet preview` takes for the run's first asset:

```console
$ facet example kokoro.speak --run
laboratorio.toml
facet run kokoro.speak /home/you/work/kokoro-speak/laboratorio.toml --out /home/you/work/kokoro-speak/out
laboratorio.wav laboratorio passed
out /home/you/work/kokoro-speak/out
preview: facet preview <first asset>
```

With `--json`, `data` carries `dest`, `piece` and `files`, and with
`--run` it adds the run's data under `data.run`, the same command as
`data.preview_command`, and stops there; a `--list` answer carries
`capability` and `examples` instead, each example with its `piece` stem
and registry `path`.

Lock: none for the copy; with `--run` the run takes the lock per the
package manifest, like `facet run` itself. The copy accepts the lock
waiting flags only because `--run` passes them through to the run.

Exit codes: `0` clean (with `--run`, the run's exit code); `2` an unknown
capability, a capability with no example, a `--name` with no example of
that stem, or an existing non-empty destination — the no-example message
names the capability and lists the capabilities of that package that do
have examples.

## `facet preview`

Shows one asset, or a whole folder as one gallery: to an agent as data, to
a person through the capability's viewer — see [preview.md](preview.md).
The preview never starts the package's server, even for a capability with
`verify_uses_server = true`. A `document` asset is different: its person
preview is the PDF itself — the printed URL is the asset's `file://` path
and the browser opens exactly that — and its `--json`
`images` array carries the page PNGs, at most 10, with the `preview`
object saying `page_count` and `truncated` one way or the other.

A fresh preview — one whose file is not older than the asset — is shown
without the package: the finished piece already holds what the viewer
needs. Only a preview that must be rebuilt asks for the package, and with
it gone fails with `not_installed` and the hint `facet pull <package>`.

The argument is optional. A folder, or no argument at all (the current
directory), previews every asset below it that has a sibling record as one
gallery, sorted by path; starting the gallery starts no runner and each
asset is `fresh`, `stale` or `missing`. Opening a `stale` or `missing`
card whose package is still installed builds its preview through the
runner's `preview` verb; a failed build answers 500 and leaves the card.

Flags: `--no-open` (print the viewer URL only), `--port PORT`,
`--idle-timeout SECONDS` (stop the server after idle; default 600),
`--kind KIND` (folder mode only: keep one asset kind).

```console
$ facet preview assets/canopy.txt --json
{"contract": 1, "data": {"asset": "assets/canopy.txt", "images": [], "preview": {"fields": {"columns": 60, "font": "dos_rebel", "rows": 9}, "kind": "text", "preview": 1, "summary": "DOS Rebel · 60×9"}, "record": {"name": "canopy", "kind": "text", "…": "the full record"}}, "facet": "0.1.0b1", "ok": true}
$ facet preview out/thesis.pdf --json              # a document: pages ride on images
{"contract": 1, "data": {"asset": "out/thesis.pdf", "images": ["/abs/out/page-01.png", "…", "/abs/out/page-10.png"], "preview": {"kind": "document", "page_count": 11, "pages": ["page-01.png", "…", "page-11.png"], "preview": 1, "summary": "a footnoted draft", "truncated": true}, "record": {"name": "thesis", "kind": "document", "…": "the full record"}}, "facet": "0.1.0b1", "ok": true}
$ facet preview assets/canopy.txt --no-open
http://127.0.0.1:39651/viewer/index.html?asset=canopy.txt
$ facet preview out/thesis.pdf --no-open           # a document prints its file URL
file:///home/facet/out/thesis.pdf
$ facet preview pieces --kind image --json         # a folder: one entry per asset
{"contract": 1, "data": {"assets": [{"asset": "caja/caja-product.png", "bytes": 48213, "capability": "conjure.image", "created": "2026-11-12T10:04:11Z", "duration_s": null, "kind": "image", "preview": {"preview": 1, "…": "the parsed preview"}, "preview_state": "fresh", "verified": true}, "…"], "folder": "pieces"}, "facet": "0.1.0b1", "ok": true}
$ facet preview pieces --no-open                    # the gallery page, one server
http://127.0.0.1:39651/gallery/index.html
```

Lock: none.

Exit codes: `0` (data, or the server started and stopped cleanly again);
`2` a missing asset, a folder with no assets left, or a record whose
capability is unknown; `7` the package is not installed and the preview
must be rebuilt; `9` its `preview` verb broke the protocol.

## `facet license`

See [licenses.md](licenses.md) for the whole picture.

```console
$ facet license list
termsy-weights CC-BY-NC-4.0 not accepted
```

- `facet license accept [<model-id>]` — accepts in the terminal. With an id,
  facet asks for that exact id back; with no id at a terminal it lists the
  pending models, takes a number (or `q`), shows the chosen terms and asks
  for the id. Enter cancels, recording nothing. The positional is optional
  and shown as `<model-id>`; off a terminal, with `--json` or with `--yes`
  it must be given.
- `facet license accept <model-id> --yes` — accepts without asking, for an
  agent a person directs.
- `facet license list` — every model requiring acceptance, with status.
  `list` takes no lock and prints nothing when none does.

`accept` holds the lock exclusively, once per acceptance. Exit codes: `0`
(recorded, cancelled at the prompt, or nothing to accept); `2` an unknown
model (lists the known ones in `details.known`), a mistyped confirmation
(nothing is recorded, and the message names the expected id), or no model
with `--yes`, `--json` or no terminal (the message names the pending ids
and `facet license accept <model-id>`); `10` `license_needs_person` when a
named model is accepted not on a terminal and without `--yes`.

## `facet remove`

Removes every environment and server or source checkout of one package. The models
stay — downloading them again is the expensive part; `--models` deletes
them too, but only those no other installed package lists.

Flags: `--models`.

```console
$ facet remove ascii
/tmp/opencode/facet-docs/envs/ascii/0.1.0
freed 86478727 bytes
```

When the package keeps its own data under `<home>/data/<package>/`, the
output adds a `kept <path> (the package's own data; delete it by hand to
discard it)` line: that directory is never deleted by facet. With `--json`
the same directory is in `data.kept`, a list that is absent when the
package has no data directory yet.

Removing an uninstalled package is a no-op with exit 0, so a script may
remove twice. Lock: exclusive.

Exit codes: `0`, always the no-op case included; `2` an unknown package
name is not an error here — a package not in the registry is a no-op too;
`6` the lock is busy and `--no-wait` was given.

## `facet clean`

Removes what nothing uses and reports what it freed. Nothing is collected
on its own; this verb is the only garbage collector.

Flags: `--dry-run` (list what would go, delete nothing). Use `--dry-run`
first: its `uv-cache` entry is the size of the whole cache, while a real
sweep reports what pruning freed.

```console
$ facet clean --dry-run
/tmp/opencode/facet-docs/cache/uv 280264681 bytes uv-cache
would free 280264681 bytes
```

It removes stale environment versions, environments of packages the
snapshot no longer has, server and source checkouts that are stale,
unfinished or of a gone package, unlisted models, old snapshots, tmp scratch, kept
leftover directories of failed runs, logs older than 30 days, and prunes
uv's cache. Environments in the active snapshot are kept, and so is every
package's own data under `<home>/data/<package>/`: that directory is never
collected. Lock: exclusive.

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
