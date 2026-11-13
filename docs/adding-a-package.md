---
title: Adding a package
description: "How to write a registry package: its manifest, runner, skill and tests."
order: 6
---

# Adding a package

A **registry** is the repository facet installs from
([UniverLab/facetmedia-registry](https://github.com/UniverLab/facetmedia-registry)).
A **package** is one directory in it, and its **manifest** is that
directory's `package.toml`. Adding a tool means adding a package directory
to the registry — never touching facet's core.

Where the two kinds of change go:

- A package that joins an **existing facet** (a new `text` package, say)
  is a registry pull request: page author, registry CI, maintainer review.
  See the registry's [CONTRIBUTING.md](https://github.com/UniverLab/facetmedia-registry/blob/main/CONTRIBUTING.md)
  for the entry criteria and the step-by-step.
- A **new facet** (a domain of creation no package has yet) arrives only
  with a reviewed facet release, because it extends facet's own facet
  list.

## The registry layout

```text
index.toml                        # the package list
packages/<name>/package.toml      # the manifest
packages/<name>/pyproject.toml    # the environment's dependencies
packages/<name>/uv.lock           # the pinned lock, authoritative
packages/<name>/runner.py         # the package runner
packages/<name>/runner.js         # a node package's entry, staged with it
packages/<name>/skill/SKILL.md    # the package skill
lib/<name>.py                     # shared modules packages may declare
viewers/<viewer>/index.html       # person-facing viewers
schemas/preview/<kind>.schema.json  # one preview schema per asset kind
```

facet reads this layout offline from one pinned commit (see
[index.md](index.md#the-registry-and-how-it-is-pinned)). `facet registry
validate <dir>` loads every part and reports **every** problem at once;
run it before opening the pull request — the registry's CI does the same.

## Bundled examples

A package may ship working pieces under `examples/` — TOML files facet's
`example` verb copies into a folder so a person has something to run and
preview. Each `.toml` at any depth under `examples/` is assigned to a
capability by the first rule that applies:

1. a top-level `capability = "<id>"` key in the file (`facet run` reads
   only `[inputs]`, so the key is harmless there) — **recommend this rule
   for new examples**, because it keeps working when stems and inputs change;
2. the file stem equals the capability's short name (`diagram.toml` is an
   example of `hyperframes.diagram`);
3. its `[inputs]` validate against exactly one capability of the package,
   through facet's existing input validation.

A file that matches none, or several capabilities under rule 3, is not an
example. Keep every file and folder an example's inputs reference beside
the piece, so the copy still runs.

## `index.toml`

```toml
version = 1

[[packages]]
name = "textured"
```

`version` is `1`. Each entry names one directory under `packages/`; names
are unique and match `[a-z][a-z0-9-]*`, and the directory's `package.toml`
`name` must equal it.

## `package.toml`

```toml
name = "textured"                     # equals the directory name
version = "0.1.0"                     # manifest and lock version, not the tool's
requires_facet = ">=0.1.0b1,<0.2"     # PEP 440 specifier against facet's own version
protocol = 1                          # runner protocol version
provider = "the upstream project · the library"   # both, separated by ·
tool_name = "Textured"
description = "One line: what this package makes."
facet = "text"                        # voice | audio | image | video | cad | text | presentation
```

Every key above is required, and text values may not be empty; unknown
keys are an error at every level, named by their dotted key. Two of them
carry facet's compatibility rules:

- `requires_facet` is evaluated with `packaging` against facet's version,
  prereleases included. An incompatible package stays in the registry but
  cannot be pulled or run: facet answers `machine_cannot` (exit 5) with
  the hint `facet update, or upgrade facet`. `facet update` also lists
  compatibility per package.
- `protocol` must equal `1`, the runner protocol this facet speaks.

`facet` names the domain of creation the package belongs to; a package
that makes speech and one that transcribes it are both `voice`. A new
facet value arrives only with a facet release.

The package's **weight** is derived from what the manifest already
declares, never declared again: `light` (no GPU, not exclusive, models
totalling at most 100 MB), `cpu-heavy` (no GPU, but exclusive or more
than 100 MB of models), `gpu`.

### `[engine]` and `[transport]`

```toml
[engine]
type = "python-env"   # or "node" / "host-app" / "binary"; see the engine sections below
python = "3.12"       # required for python-env, forbidden for node, host-app and binary

[transport]
type = "cli"          # the only transport in v0.1
entry = "runner.py"   # relative to the package directory for every engine but binary, no ".."
```

`invocation_template` may be omitted; the default is
`{{python}} -I {{entry}} {{verb}} --input {{input}} --result {{result}}`.

### `[requirements]`

```toml
[requirements]
gpu = false           # default false
ram_gb = 2            # default 0
vram_gb = 0           # default 0; needs gpu = true when above 0
disk_gb = 1.2         # default 0
exclusive = true      # default true: a run takes the facet lock exclusively
platforms = ["linux"] # default ["linux"]
host_libs = []        # shared libraries by soname, e.g. "libGL.so.1"
host_hint = ""        # how a person installs them
```

facet never installs host libraries; `host_hint` is what the readiness
check prints, and `exclusive = false` is what lets several light runs
overlap. `vram_gb` above 0 needs `gpu = true`; `doctor`, `pull` and `run`
refuse a machine whose GPU reports less VRAM than the package needs.

### Tier groups

Two optional top-level keys mark packages that are alternatives of one
tool:

```toml
tier_group = "comfy"  # packages sharing it are alternatives
tier = "standard"     # unique within the group, e.g. "standard" or "xl"
```

Both keys are given together or neither is, and `facet registry validate`
rejects a duplicate `tier` in one group. `facet pull` includes exactly one
member of each group: the one with the highest declared `vram_gb` that
fits, and `--tier NAME` prefers that tier even when it does not fit.

### `[[licenses]]`

One entry for the tool and each notable dependency in its environment:

```toml
[[licenses]]
subject = "ezdxf"
spdx = "MIT"
```

### `[[models]]`

Model ids are unique across the whole registry. A model is fetched either
from a direct URL or from a Hugging Face repository, never both:

```toml
[[models]]
id = "my-weights-v1"
spdx = "CC-BY-NC-4.0"
commercial_use = false          # default false
requires_acceptance = true      # true for NC models or models with terms
terms_url = "https://example.com/terms"
url = "https://example.com/my-weights.safetensors"   # https:// or file://
sha256 = "<64 lowercase hex characters>"
bytes = 325505369               # required, positive
path = "my-weights-v1/my-weights.safetensors"        # relative, no ".."
```

Or from Hugging Face, replacing `url` and `sha256` with:

```toml
hf_repo = "Systran/faster-whisper-small"
revision = "<40-hex commit>"
```

`bytes` is required in both forms — `pull` checks sizes before and after a
download, models need no acceptance when `requires_acceptance = false`,
and a model needing acceptance fails every gate with the exact
`facet license accept <id>` command (see [licenses.md](licenses.md)).

A model the backend serves instead of facet fetching declares no source at
all — `backend = true` with no `url`, `sha256`, `path`, `hf_repo`,
`revision` or `bytes` (naming any of them is a manifest error):

```toml
[[models]]
id = "omnivoice-v1"
spdx = "CC-BY-NC-4.0"
commercial_use = false          # default false
requires_acceptance = true      # true for NC models or models with terms
terms_url = "https://example.com/omnivoice-terms"
backend = true                  # served by the backend: never fetched, never hash-checked
```

`pull` skips a backend-owned model entirely and reports it as
`backend-owned`; `run` still requires acceptance for the pieces that select
it (see [licenses.md](licenses.md)).

## Shared modules

Packages run in separate environments, so a helper they both need is kept
once in the registry and copied into each environment at pull time:

```text
lib/<name>.py    # <name> matches [a-z][a-z0-9_]*
```

A module starts with a docstring whose first line is a one-line summary,
and carries a line naming the distributions it imports beyond the
standard library — or exactly `stdlib`:

```python
"""Normalise text before synthesis.

Requires: charset-normalizer, regex
"""
```

A package declares the modules it uses in an optional top-level key of its
`package.toml`:

```toml
uses = ["textnorm"]
```

`facet registry validate` rejects a name with no `lib/<name>.py`, a
duplicate, and a module whose `Requires:` names a distribution that is
absent from the package's `pyproject.toml` dependencies. Distribution
names are compared normalised, the PEP 503 way, so `Charset_Normalizer`
matches `charset-normalizer`.

At pull time each declared module lands in the environment's site-packages
as `facet_lib/<name>.py`, next to an empty `facet_lib/__init__.py`, and a
runner imports it with `from facet_lib import textnorm`. Only declared
modules are copied. A package that declares no `uses` gets nothing new,
exactly as before.

The environment remembers what it copied in a `.registry` marker next to
`.complete`. The fingerprint in that marker is the pinned snapshot commit
when the registry is a snapshot, or, with `FACET_REGISTRY` set, an
`override:<sha256>` over the used modules (sorted by name) and the
package's `package.toml`. A changed lock hash **or** a changed fingerprint
makes the environment stale, so the next `facet pull` rebuilds it and
rewrites `.complete` and `.registry`. `facet doctor` reports a stale copy
with the fix `facet pull <package>`; that fix never gates a pull or a run.

Beside those two markers the environment carries a third one, `.package`:
a sha256 over the sorted relative paths plus the bytes of every file under
the package directory (`__pycache__/` and `*.pyc` excluded). It covers the
whole package — the runner, helper modules, flows, scenes and skill files —
so a registry edit that keeps the version, the lock and the entry file
still triggers a rebuild: a missing or differing `.package` reads as stale
once, and the next `facet pull` — or a `facet run`, which pulls a stale
package the way it pulls an absent one — rebuilds from scratch and rewrites
it. `facet run --no-pull` refuses a stale package with `not_installed`, and
so does `facet check`: neither verifies with stale code.

## Browser artifact

A package that renders — presentation slides, the video facet's render
path — needs a real Chromium, and v0.1 has no browser engine of its own.
The package declares the build it needs in an optional top-level
`[browser]` table of its `package.toml`:

```toml
[browser]
channel = "chromium"                  # the only channel in v0.1
revision = "1254"                     # a Playwright-compatible build revision
version = "158.0.8127.18"             # the Chrome for Testing version
sha256 = "<64 lowercase hex>"         # of the official Chrome for Testing archive
```

`facet registry validate` rejects an unknown channel, a missing `revision`,
a missing `version`, a `version` that is not four dot-separated integers,
and a `sha256` that is not 64 lowercase hex characters.

`facet pull` downloads `chrome-linux64.zip` for the declared `version` from
the official Chrome for Testing CDN
(`https://cdn.playwright.dev/builds/cft/<version>/linux64/chrome-linux64.zip`),
with the same three attempts and backoff as
model downloads, verifies the archive's sha256, and extracts it once at
`<home>/browsers/chromium-<revision>/` — Playwright's own
`<name>-<revision>` layout, the same one `~/.cache/ms-playwright` uses,
never a nested `<channel>/<revision>` directory. The executable lands at
`chromium-<revision>/chrome-linux64/chrome`. An extraction whose
`.facet-verified` marker does not hold the declared sha256, or whose
executable is missing, is fetched again;
a verified one is never re-downloaded.

Any engine may declare it: a `python-env`, `node`, `binary` or `host-app`
package's pull fetches the browser before its own engine step.

The browser is shared: one extraction per channel and revision under
`<home>/browsers`, never per package and never inside an environment —
environments are rebuilt, the browser is not. The runner environment gets
`PLAYWRIGHT_BROWSERS_PATH` pointing at `<home>/browsers`, so the package's
own `playwright` pip dependency finds the browser without downloading
anything itself, and `FACET_BROWSER` with the absolute path of the
`chrome-linux64/chrome` executable, so drivers that take an explicit
`executablePath` (puppeteer, Playwright) need no host Chrome.
Browser builds are only available on Linux x86-64; on any other host `pull`
of a package declaring `[browser]` refuses with `machine_cannot` (exit 5).
`facet doctor` reports a declared browser whose extraction
is missing or unverified with the fix `facet pull <package>`; like
`installed`, `models` and `shared_modules`, that check never gates the pull
that fixes it.

## FFmpeg artifact

A package that encodes video — the video facet's encode path — needs a
real FFmpeg with `ffprobe`, and v0.1 has no video engine of its own. The
package declares the build it needs in an optional top-level `[ffmpeg]`
table of its `package.toml`:

```toml
[ffmpeg]
version = "n8.1.3-27-g8dca1586f4"   # the static build's version
url = "https://github.com/FFmpeg-Builds/FFmpeg-Builds/releases/download/…/ffmpeg-n8.1.3-27-g8dca1586f4-linux64-gpl-8.1.tar.xz"
sha256 = "<64 lowercase hex>"       # of the upstream `.tar.xz` archive
```

`facet registry validate` rejects a missing `version`, `url` or `sha256`,
a `url` that does not start with `https://`, a `sha256` that is not 64
lowercase hex characters, and any unknown key.

`facet pull` downloads the archive from the declared `url`, with the same
three attempts and backoff as model downloads, verifies the archive's
sha256, and extracts it once at `<home>/ffmpeg/<version>/`, stripping the
archive's single top directory: the binaries land at
`<version>/bin/ffmpeg` and `<version>/bin/ffprobe`, and a
`.facet-verified` marker holds the declared sha256. An extraction whose
marker does not hold the declared sha256, or whose `bin/ffmpeg` or
`bin/ffprobe` is missing, is fetched again; a verified one is never
re-downloaded. An archive without exactly one top directory, or without
both binaries, fails the pull with `download_failed`.

Any engine may declare it: a `python-env`, `node`, `binary` or `host-app`
package's pull fetches the FFmpeg build before its own engine step, right
after `[browser]`.

The FFmpeg build is shared: one extraction per version under
`<home>/ffmpeg`, never per package and never inside an environment —
environments are rebuilt, the FFmpeg extraction is not. The runner
environment gets `FACET_FFMPEG` and `FACET_FFPROBE` with the absolute
paths of the two binaries, and `<home>/ffmpeg/<version>/bin` prepended to
`PATH`, so tools that call `ffmpeg` by name use the shared build.
FFmpeg builds are only available on Linux x86-64; on any other host
`pull` of a package declaring `[ffmpeg]` refuses with `machine_cannot`
(exit 5). `facet doctor` reports a declared FFmpeg build whose extraction
is missing or unverified with the fix `facet pull <package>`; like
`installed`, `models` and `shared_modules`, that check never gates the
pull that fixes it.

The build itself is GPL-3.0: `facet describe` and run records list it
through the package's own `[[licenses]]`, the way every dependency
licence is declared there.

## The document kind

Long written documents — the texforge thesis, articles, reports — are a
kind of their own. A `document` capability writes a multi-page PDF
(`kind = "document"`, `mime = "application/pdf"`), and a finished
document package needs all of it: the schema, `run`, `verify`, `preview`
(person and agent), `doctor` and its skill.

The agent preview is the schema's `document.preview.json`: the runner's
`preview` verb produces one PNG per page, in page order, into the asset's
directory, and writes `pages` (the list of PNG names) and `page_count`:

```json
{"preview": 1, "kind": "document", "summary": "…", "page_count": 12, "pages": ["page-01.png", "…", "page-12.png"]}
```

The runner renders the pages itself: the reference mechanism is the
shared [browser artifact](#browser-artifact) this manifest declares via
`[browser]`, screenshotting each PDF page at roughly 150 dpi. facet
installs no renderer of its own — no system poppler, no PDF library in
facet itself; the pages are the browser's work, produced by the package.

To a person, `facet preview` of a document opens the PDF itself: facet
prints the asset's `file://` URL and opens it with the platform opener,
there is no viewer and no server (see
[preview.md](preview.md)). To an agent, the page PNGs ride the `--json`
`images` array, at most 10 pages, with `truncated` marking an 11+-page
book.

The package's `verify` verb carries the kind's own contract: it parses
the PDF, opens every page PNG beside the asset, reads the sibling record
file, and compares its fresh `page_count` metric against the recorded
one — a missing or extra page fails the verdict, and `facet check`
reports it. All parsing stays in the package: core dependencies are
`uv` and `packaging` only, and they stay that way.

## Server engine

Some tools cannot be pip-installed: they are cloned at a pinned commit, built
in place, and serve a local API. A package like that keeps its thin client as
the ordinary `python-env` engine with `transport = "cli"` and adds a
`[engine.server]` table:

```toml
[engine]
type = "python-env"                   # the only engine that carries a server
python = "3.12"

[engine.server]
repo = "https://github.com/example/tool"   # https only
rev = "<40 lowercase hex>"                 # the exact commit
requirements = "requirements.txt"          # or "uv.lock", the tool's own file, in the checkout
launch = ["python", "main.py", "--port", "{{port}}"]
health_url = "http://127.0.0.1:{{port}}/health"   # 200 means ready
timeout_s = 120                            # startup wait; optional, defaults to 120
env = { TOOL_DATA_DIR = "{{data}}/tool" }  # optional extra environment, see below
```

Each `env` key must match `^[A-Z_][A-Z0-9_]*$` and must not be `PATH`,
`PORT`, or start with `FACET_`; each value must be a string holding only
the `{{data}}` and `{{port}}` tokens. `{{data}}` resolves to
`<home>/data/<package>/`, created on first use, and `{{port}}` to the
port facet picked. `<home>/data/<package>/` is the package's own data:
no facet command — not `remove`, `clean`, `update`, nor pruning old
versions — ever deletes it.

There is no `type = "server"` engine: the runner stays a thin `cli` client
that talks to the server facet started for it, and `facet registry validate`
rejects `[engine.server]` on any engine or transport but `python-env` + `cli`,
a non-https `repo`, a `rev` that is not 40 lowercase hex characters, a
`requirements` that is missing or leaves the checkout, a `launch` argv without
`{{port}}`, and a `health_url` without a scheme.

A capability whose `verify` verb needs the backend opts in with
`verify_uses_server = true` in its `[capabilities.<name>]` table (a boolean,
default `false`):

```toml
[capabilities.profile]
summary = "…"
kind = "audio"
verify_uses_server = true
```

`facet registry validate` rejects a non-boolean value, and rejects the key
on a package without `[engine.server]`. `facet check` starts the package's
server for verify only when the asset's capability declares
`verify_uses_server = true`; otherwise the runner gets no `FACET_SERVER_URL`,
the same environment as a quick doctor. `facet preview` never starts the
server. `facet describe --json` shows the flag as `verify_uses_server`, so
an agent knows a check will boot the backend.

`facet pull` first builds the client environment exactly as for any other
package, then clones the repo once per home at
`<home>/servers/<package>/<version>/<shortrev>/` — a shallow, branchless fetch
of exactly `rev`, skipped when that directory already holds a verified
checkout. A pip requirements file builds the server venv with
`uv venv --python <engine.python>` and installs the tool's requirements into
it with `uv pip install -r`, untouched by facet. `requirements = "uv.lock"`
instead builds from the upstream project's own lock with
`uv sync --frozen --no-dev --project <checkout> --python <engine.python>`
with `UV_PROJECT_ENVIRONMENT=<checkout>/venv`, which creates the venv itself.
Prefer the lock when the upstream requirements file pulls packages that need a
build step, as ACE-Step's does with flash-attn. A `.complete` marker holding
the rev and that file's sha256 is written last, so an interrupted pull is
redone by the next one.
`installed` means both the client environment and the server checkout are
complete; `facet remove` and `facet clean` free the checkout tree like an
environment.

A server is per run, never a daemon: `facet run` probes `health_url`, and when
nothing answers it starts `launch` with the checkout as cwd, the server venv
first on `PATH`, `PORT` set to the port facet picked, and the manifest's own
`env` entries resolved (`{{data}}` and `{{port}}` substituted), waits for a
200 up to `timeout_s`, runs the client with `FACET_SERVER_URL` set to the
server's base URL (the scheme, host and port of `health_url`, no path), and
always kills the server's whole process group afterwards. A launch that never
turns healthy fails `runner_failed` with its last output lines. The client
reads `FACET_SERVER_URL`; it never starts, stops or holds the tool.

## Source engine

Some tools ship Python code that is not pip-installable and serves no API:
it is imported, not launched. A package like that keeps the ordinary
`python-env` engine with `transport = "cli"` and adds an `[engine.source]`
table:

```toml
[engine]
type = "python-env"                   # the only engine that carries a source
python = "3.12"

[engine.source]
repo = "https://github.com/example/tool"   # https only
rev = "<40 lowercase hex>"                 # the exact commit
```

`facet registry validate` rejects `[engine.source]` on any engine or
transport but `python-env` + `cli`, a non-https `repo`, a `rev` that is not
40 lowercase hex characters, and a package that declares both
`[engine.server]` and `[engine.source]`: a package clones either a server
or a source checkout, never both.

`facet pull` clones the repo once per home at
`<home>/servers/<package>/<version>/<shortrev>/` — a shallow, branchless
fetch of exactly `rev`, skipped when that directory already holds a verified
checkout. A `.complete` marker holding the rev is written last, so an
interrupted pull is redone by the next one. `installed` means both the
client environment and the source checkout are complete; `facet remove` and
`facet clean` free the checkout tree like a server checkout.

The runner finds the checkout in `FACET_SOURCE_DIR` and puts it on
`sys.path` itself; facet never imports the tool. The tool's own
third-party dependencies go in the package's `pyproject.toml`, as for any
other `python-env` package.

## Node engine

Some tools are Node CLIs, not Python packages. A package like that declares
`type = "node"` and pins both the runtime and every npm tarball it needs:

```toml
[engine]
type = "node"

[engine.node]
runtime = "22.21.0"                   # exact Node version
runtime_sha256 = "<64 lowercase hex>" # of nodejs.org's linux-x64 tarball

[[engine.node.packages]]
name = "@scope/tool"                  # the npm name
version = "1.2.3"                     # semver
tarball = "https://registry.npmjs.org/@scope/tool/-/tool-1.2.3.tgz"
integrity = "sha512-<base64>"         # the tarball's SRI hash
```

The FIRST `[[engine.node.packages]]` entry is not the entry script: it is
the first package whose extracted tool CLIs the staged script spawns or
requires, and every declared package is exported on `FACET_NODE_PATH` in
declaration order. The entry script itself lives in the package directory,
exactly like a Python runner. `engine.python` is required for
`python-env` and forbidden here. `facet registry validate` rejects a
non-semver runtime or package version, a malformed `runtime_sha256`, an
`integrity` that is not a valid SRI hash, a `tarball` whose host is not
`registry.npmjs.org`, and a node package whose entry file is missing.

`facet pull` downloads `node-v<runtime>-linux-x64.tar.gz` from nodejs.org
and each package tarball from registry.npmjs.org — the same three attempts
and backoff as models — verifies the runtime's sha256 and each tarball's
integrity, and extracts under the facet home: the runtime at
`<home>/runtimes/node/<version>/` (its `bin/node` is the binary facet
runs) and each package at `<home>/node_pkgs/<name>/<version>/` with the
tarball's `package/` contents at the root. Each extraction carries a
`.facet-verified` marker written last; a verified extraction is never
re-downloaded. There is no npm and no npx anywhere: nothing is installed
outside the facet home, no script runs at install time, and once pulled the
package runs with no network at all.

The pull also stages the package directory at
`<home>/envs/<package>/<version>/package/` — the whole directory, so
helper files the entry requires relatively come with it. `<env>/.complete`
holds the **sha256 of the source entry file's bytes**, and
`<env>/.package` holds the fingerprint over every file under the package
directory (`__pycache__/` and `*.pyc` excluded); both are written last,
after the copy, and a later pull restages the whole tree from scratch when
either marker no longer matches, so an edited entry — or an edited helper,
flow, scene or skill file at the same version — is picked up and an
interrupted copy is redone. The staged copy is never merged into.

Every node package writes its template with `{{node}}`, which resolves to
the managed binary, mirroring `{{python}}`:

```toml
[transport]
type = "cli"
entry = "runner.js"       # relative to the package directory, staged with it
invocation_template = "{{node}} {{entry}} {{verb}} --input {{input}} --result {{result}}"
```

The runner environment additionally exports `FACET_NODE` (the managed
`bin/node`) and `FACET_NODE_PATH` (the declared package directories,
`:`-joined) for scripts the entry spawns. `facet doctor` reports a missing
or unverified extraction, or a staged runner that is not current, with the
fix `facet pull <package>`; a verified runtime the host cannot execute is a
`machine_cannot`, not a pull problem.

## Binary engine

Some tools are single static binaries — no Python environment, no server,
no host install. A package like that declares `type = "binary"` and pins
one release archive, which is always a **zip** (extracted with the
standard library's `zipfile`, the browser precedent):

```toml
[engine]
type = "binary"

[engine.binary]
name = "rhubarb"                    # the extraction's directory name
version = "1.14.0"                  # the release version
url = "https://example.com/rhubarb-1.14.0-linux-x64.zip"  # https only
sha256 = "<64 lowercase hex>"       # of the zip archive
bin = "rhubarb"                     # executable inside the zip, possibly nested
version_arg = "--version"           # default; how facet checks the version
```

`engine.python` is forbidden here, exactly as for `node`, and
`[transport].entry` is forbidden too: there is no entry script. Every
binary package writes its `invocation_template` with the `{{binary}}`
token — `facet registry validate` rejects a template without it, a
non-https `url`, a malformed `sha256`, an empty `bin`, and a `name` or
`version` that is not a single directory name. Versions live in package
manifests, never in the core.

`facet pull` downloads the zip — the same three attempts and backoff as
models — verifies its sha256, and extracts it under
`<home>/bin/<name>/<version>/`. The executable bit is set on `bin`, and a
`.facet-verified` marker holding the archive's sha256 is written last; a
verified extraction is never re-downloaded, and a tampered archive fails
`download_failed` with nothing left behind.

`{{binary}}` is owned by the runner, not the template: it resolves to
`<home>/bin/<name>/<version>/<bin>` and the runner prepends it as
`argv[0]`, exactly as `{{node}}` resolves to the managed runtime:

```toml
[transport]
type = "cli"
invocation_template = "{{binary}} {{verb}} --input {{input}} --result {{result}}"
```

Before every run the runner re-probes `<bin> <version_arg>`: a missing
extraction or a version output that does not contain the declared version
fails `machine_cannot`, so a direct run can never execute the wrong
binary. The runner environment prepends the resolved executable's parent
directory to `PATH` — `bin` may sit nested beside sibling libraries — and
exports `FACET_BIN` with the resolved binary's absolute path, mirroring
`FACET_NODE`. `facet doctor` runs the same probe: a missing extraction
fails with the fix `facet pull <package>`; a verified binary that cannot
run, or whose version output misses the declared version, is a
`machine_cannot`, not a pull problem.

## Host-app engine

Blender and FreeCAD cannot be isolated: they are host applications driven
by a script. A package like that declares `type = "host-app"` and names the
application, the minimum version it needs, and where a person's
configuration stores the binary's path:

```toml
[engine]
type = "host-app"

[engine.host_app]
app = "blender"          # blender | freecad, the only values
min_version = "4.0"      # dotted numeric: X.Y or X.Y.Z
config_key = "blender.path"   # default: <app>.path
env = "BLENDER_EXE"      # default: BLENDER_EXE for blender, FREECAD_EXE for freecad
```

`engine.python` is required for `python-env` and forbidden here, exactly as
for `node`: a host-app package has no python environment. `facet registry
validate` rejects any other `app`, a non-numeric `min_version`, a
`config_key` that is not dotted, and an `env` that is not an environment
variable name. A two-component minimum such as `4.0` or `1.1` is accepted
and compares as `4.0.0` / `1.1.0`.

A host application is **never installed**. The binary is located in this
order:

1. the config key's value in `<home>/config.toml`, written by
   `facet config set blender.path /path/to/blender`;
2. the `env` variable, the lab's existing convention;
3. `PATH` (`blender`; for FreeCAD, `freecadcmd` then `freecad`).

`facet pull` downloads nothing and builds no environment: it resolves the
binary, probes `<binary> --version`, and compares the first dotted version
it prints against `min_version`. `is_installed` is true exactly when the
resolution and the version check pass. A missing application fails
`machine_cannot` with every location it tried, and a too-old one names the
found version, the minimum, and where it found the binary:

```text
blender 4.0+ not found (config blender.path, env BLENDER_EXE, PATH all empty); fix: facet config set blender.path /path/to/blender
blender 3.1 found, needs 4.0+ (config blender.path); fix: facet config set blender.path /path/to/blender
```

`facet doctor` reports the same failure with the
`facet config set <config_key> /path/to/<app>` fix. It is not pull-fixable:
no pull changes it, so `pull` and `run` refuse through the doctor gate.

The template uses `{{app}}` for the resolved host binary and `{{entry}}` for
the package's script; `{{python}}` does not apply. The runner injects the
application's failure semantics itself and appends the template's remaining
arguments after them:

```toml
[transport]
type = "cli"
entry = "render.py"
invocation_template = "{{app}} {{entry}} --input {{input}} --result {{result}}"
```

- `blender` runs
  `<binary> -b --factory-startup --python-exit-code 1 --python <entry> ...`.
  The exit-code flag precedes the script flag on purpose: Blender handles
  both in command-line order, and a later flag registers too late to turn a
  script that raises into a failing run.
- `freecad` runs `<binary> <bundled freecad_run.py> --pass <entry> ...`,
  where the bundled wrapper (port of the lab's `freecad_run.py`) executes
  the script with `runpy` and exits with its code, because `freecadcmd`
  exits 0 and prints nothing when a script raises.

A failing script therefore fails the run; it is never silent. When the
resolved binary is a Windows `.exe` under WSL, path arguments — the script,
the wrapper, `{{input}}`, `{{result}}` and any remaining argument containing
`/` — cross through `wslpath -w`, as the lab's scripts do.

Runners always receive absolute `out_dir` and `piece_dir` in the request,
whatever the caller passed, so a Windows host resolves them the same way
facet does. For a Windows `.exe` host, facet also forwards its own variables
through `WSLENV`, which is the only environment a WSL-launched `.exe` sees:
path variables (`FACET_MODELS`, `HF_HOME`, and, when set, `FACET_HOME`,
`FACET_BIN` and `PLAYWRIGHT_BROWSERS_PATH`) with the `/p` flag, and
`FACET_SERVER_URL` and `CUDA_VISIBLE_DEVICES` without a flag. Any `WSLENV`
already present is kept, and no variable is listed twice.

## Capabilities and their inputs

Each capability is `[capabilities.<name>]`, its id is
`<package>.<name>` (for example `textured.strip`), and `<name>` matches
`[a-z][a-z0-9-]*`:

```toml
[capabilities.strip]
summary = "One line, shown by facet list and describe."
kind = "text"   # audio | video | image | cad | presentation | text | document
```

`kind` is the asset the capability writes; it alone picks the preview
schema and the viewer (`textured` tools make `text`; long written
documents make [`document`](#the-document-kind)).

Inputs are fields under `[capabilities.<c>.inputs.<field>]`:

```toml
[capabilities.strip.inputs.width]
type = "integer"
default = 80
min = 10
max = 300

[capabilities.strip.inputs.text]
type = "string"
required = true
pattern = "^[^\\n]+$"
```

`type` is required, one of `string`, `integer`, `number`, `boolean`,
`enum` (with `values`), `path` (resolved relative to the piece file, must
exist) and `array` (one level deep, of objects described by `items`).
Optional keys are `required`, `default`, `description`, `min`, `max`,
`pattern` (fullmatch), `min_items` and `values`; each option is only
valid on its types. A `pattern` field's `description` is the rule a caller
sees when its value fails the pattern, so state the rule in words there
and never the regular expression. Verify thresholds and the viewer:

```toml
[capabilities.strip.verify]
thresholds = { columns_max = 300 }

[capabilities.strip.preview]
viewer = "text"     # the viewer must exist under viewers/
```

Every capability declares its `[capabilities.<c>.preview]` table —
`validate` rejects a capability without one, for every kind: no kind
ships without an agent preview. Omitting `viewer` keeps `generic`.

A capability may declare exactly which manifest models it uses:

```toml
[capabilities.profile]
summary = "Register the speaker's own voice."
kind = "audio"
models = []
```

`models` lists `[[models]]` ids from the same package; anything else is
rejected (`unknown model <id>`), as is a non-list or a non-string entry.
The gate follows the declaration: the piece needs acceptance only for the
listed models, so an empty list needs no acceptance at all. A capability
without `models` keeps the package-wide gate. A capability with a `model`
input never declares `models`: the piece selects its model, so the gate
follows the piece instead. `facet describe` lists the same models the gate
would ask for: the declared ones, or the package's when the capability has
a `model` input or declares nothing.

## The runner protocol

Registry runners are code that arrives from the network, so facet never
evaluates a piece: the piece is data, the tool runs inside the package's
own uv environment, and the conversation between facet and a runner is a
file protocol. facet writes a **request file**,
`{"protocol": 1, "verb", "capability", "inputs", "out_dir", "piece_dir",
"models", "assets"}` (a `doctor` request adds `"depth": "quick" |
"full"`), and the runner writes a **result file**: for `run`, its tool and
one entry per asset; for `verify`, one verdict per asset; for `doctor`, a
`{"name", "ok", "detail", "fix"}`-list of checks. A `file` is one bare
name inside the run's temporary directory, never a path and never the
reserved `.facet-` prefix. The runner is invoked through the manifest's
`transport.invocation_template` with `{{python}}` (the environment's own
interpreter), `{{entry}}`, `{{verb}}`, `{{input}}` and `{{result}}`, in
its own process group, with `FACET_MODELS` pointing at the models
directory.

The `facet_runner_sdk` module ships with facet and is copied into every
environment; the registry's shared modules, when a package declares
`uses`, are copied the same way as `facet_lib`, so a runner may
`from facet_lib import <name>`. A package runner otherwise writes tool
code and nothing else:

```python
from facet_runner_sdk import Request, Runner

runner = Runner()


@runner.verb("run")
def run(req: Request) -> dict:
    text = req.inputs["text"]
    out = req.out_dir / "strip.txt"
    ...  # write the asset with the real tool
    return {
        "tool": {"name": "textured", "version": "0.1.0"},
        "assets": [
            {
                "file": "strip.txt",
                "name": "strip",
                "kind": "text",
                "mime": "text/plain",
                "properties": {...},
                "params": {...},
                "seed": None,
                "inputs": [],
            }
        ],
    }


runner.main()
```

`req` carries the request: `inputs` (validated, defaults filled, paths
absolute), `out_dir`, `piece_dir`, `assets` (on `verify` and `preview`),
`depth` (on `doctor`) and `model_path(id)`. An unknown verb or an
exception exits 1 with the reason on stderr, which facet turns into
`runner_failed` — with the one exception of a missing `doctor` verb,
which the SDK answers politely so `facet doctor` can name the fix.

## The four verbs plus doctor, plus skill

A package is finished only with all of these:

- its input `schema`, validated before anything runs, so an invalid piece
  never wastes a machine's time;
- `run` — makes the assets and declares their kind and properties;
- `verify` — judges its own assets against the capability's thresholds;
  `facet check` calls it again, so it must be pure and repeatable;
- `preview` — writes `<asset>.preview.json`, matching the kind's schema
  under `schemas/preview/`;
- its **`doctor`** at the `quick` and `full` depths, with a fix for every
  failing check it may report:

  ```python
  @runner.verb("doctor")
  def doctor(req: Request) -> dict:
      checks = []
      try:
          import thetool
      except ImportError as exc:
          checks.append(
              {
                  "name": "imports",
                  "ok": False,
                  "detail": f"the tool does not import: {exc}",
                  "fix": "facet pull textured",
              }
          )
      ...
      return {"checks": checks}
  ```

  `quick` must finish in about 10 s and write nothing (imports, files,
  versions); `full` may take up to 30 s and may write one throwaway
  output into `req.out_dir`, which facet deletes. facet enforces both
  timeouts, `pull` and `run` gate on the quick depth, and
  `facet registry validate` refuses a runner that does not register
  `doctor`.

- its **skill** — `skill/SKILL.md` plus optional
  `skill/references/<ref>.md`. `SKILL.md` opens with a front-matter block
  between two `---` lines holding exactly `name:` (the package name) and
  `description:` (one line, 20 to 200 characters, saying when an agent
  should use the package), and its body has these level-2 headings, in
  this order: `## When to use`, `## Pieces` (one complete piece example
  per capability in a fenced `toml` block), `## Judging the result`,
  `## Pitfalls`. Every capability id of the package appears in the body.
  Skills are never installed; agents read them live with
  [`facet skill <package>`](commands.md#facet-skill).

`facet registry validate` checks the skill's front matter and headings,
every viewer a capability names, unique model ids, and each runner
entry's verb registrations. Exit `0` when the registry is clean and `2`
otherwise; make that check part of the package's CI before the pull
request goes up.

## `facet_runner_sdk` in one breath

Copied into every environment as `facet_runner_sdk` at pull time, so the
runner a registry ships is the runner that runs — one import, four verbs
at most, and the same protocol on every package. The lab keeps the SDK
with facet's core; a package never vendors a copy.
