# The registry

A **registry** is the repository facet installs from
([UniverLab/facetmedia-registry](https://github.com/UniverLab/facetmedia-registry)).
A **package** is one directory in it, and its **manifest** is that directory's
`package.toml`. Adding a tool means adding a package directory to the registry,
never touching facet's core.

facet reads a registry snapshot it already downloaded, offline. `facet update`
downloads it at one pinned commit; validating a checkout is `facet registry
validate`.

## Layout

```text
index.toml
packages/<package>/package.toml      # the manifest
packages/<package>/pyproject.toml    # the environment's dependencies
                                     # ([tool.uv] package = false)
packages/<package>/uv.lock           # the pinned lock, authoritative
packages/<package>/runner.py         # the package runner
packages/<package>/skill/SKILL.md    # the package skill (see skill.md)
viewers/<viewer>/index.html          # person-facing viewers
```

A package is finished with its input `schema`, the runner verbs `run`,
`verify` and `preview`, its `doctor` — the package's own answer to
[`facet doctor`](doctor.md) at the `quick` and `full` depths — and its
[`skill`](skill.md): `SKILL.md` plus optional `skill/references/<ref>.md`.
The runner SDK
(`facet_runner_sdk`, copied into every environment) is how a runner declares
them; see [run.md](run.md#the-runner-protocol) for the protocol and
[doctor.md](doctor.md#writing-a-packages-doctor-with-the-sdk) for writing the
doctor.

## `index.toml`

```toml
version = 1

[[packages]]
name = "kokoro"

[[packages]]
name = "whisper"
```

`version` must be `1`. Each entry names one directory under `packages/`; names
are unique and match `[a-z][a-z0-9-]*`. The package's `package.toml` must exist
and its `name` must equal the directory name.

## `package.toml`

```toml
name = "kokoro"                       # [a-z][a-z0-9-]*, equals the directory name
version = "0.1.0"                     # the manifest and lock version, not the tool's
requires_facet = ">=0.1.0b1,<0.2"     # PEP 440 specifier
protocol = 1                          # runner protocol version
provider = "hexgrad · thewh1teagle"
tool_name = "Kokoro TTS (kokoro-onnx)"
description = "Local text to speech."
facet = "voice"                       # voice | audio | image | video | cad | text | presentation
```

These keys are required, and the text values may not be empty. Unknown keys
are an error at every level, named by their dotted key. `facet` names the
domain of creation the package belongs to; a package that makes speech and
one that transcribes it are both `voice`. A new facet arrives only with a
reviewed facet release. The list lives beside `CAPABILITY_KINDS`, while
`kind` keeps meaning the asset a capability writes (it alone picks the
preview schema and the viewer). The package's **weight** is derived from
what the manifest already declares, never declared again: `light` (no GPU,
not exclusive, models totalling at most 100 MB), `cpu-heavy` (no GPU, but
exclusive or more than 100 MB of models), `gpu`.

### `[engine]`

```toml
[engine]
type = "python-env"   # the only engine in v0.1
python = "3.12"
```

### `[transport]`

```toml
[transport]
type = "cli"          # the only transport in v0.1
entry = "runner.py"   # relative to the package directory, no ".."
```

`invocation_template` may be omitted; the default is:

```text
{{python}} -I {{entry}} {{verb}} --input {{input}} --result {{result}}
```

### `[requirements]`

```toml
[requirements]
gpu = false           # default false
ram_gb = 2            # default 0
disk_gb = 1.2         # default 0
exclusive = true      # default true: a run takes the lock exclusively
platforms = ["linux"] # default ["linux"]
host_libs = []        # shared libraries by soname, e.g. "libGL.so.1"
host_hint = ""        # how a person installs them
```

### `[[licenses]]`

One entry for the tool and each notable dependency in its environment:

```toml
[[licenses]]
subject = "kokoro-onnx"
spdx = "MIT"
```

### `[[models]]`

Model ids are unique across the whole registry. A model is fetched either from
a direct URL or from a Hugging Face repository, never both:

```toml
[[models]]
id = "kokoro-82m-v1.0-onnx"
spdx = "Apache-2.0"
commercial_use = true          # default false
requires_acceptance = false    # true for NC models or models with terms
terms_url = "https://huggingface.co/hexgrad/Kokoro-82M"
url = "https://example.com/kokoro-v1.0.onnx"  # https:// or file://
sha256 = "<64 lowercase hex characters>"
bytes = 325505369              # required, positive
path = "kokoro-82m-v1.0-onnx/kokoro-v1.0.onnx"  # relative, no ".."
```

Or from Hugging Face:

```toml
hf_repo = "Systran/faster-whisper-small"
revision = "<40-hex commit>"
```

`bytes` is required in both forms.

### `[capabilities.<name>]`

A capability id is `<package>.<name>`, for example `kokoro.speech`. Capability
names (like package and field names) match `[a-z][a-z0-9-]*`:

```toml
[capabilities.speech]
summary = "Text to speech."
kind = "audio"   # audio | video | image | cad | presentation | text
```

These fields are what `facet list` and `facet describe` report; see
[discover.md](discover.md) for the discovery output.

`[capabilities.<c>.verify]` holds numeric thresholds, for example:

```toml
[capabilities.speech.verify]
thresholds = { wer_max = 0.05 }
```

`[capabilities.<c>.preview]` names the viewer directory; it defaults to
`generic` and must exist under `viewers/`:

```toml
[capabilities.speech.preview]
viewer = "audio"
```

## Input schema

Each capability declares its inputs as fields under
`[capabilities.<c>.inputs.<field>]`:

```toml
[capabilities.speech.inputs.voice]
type = "string"
default = "ef_dora"

[capabilities.speech.inputs.text]
type = "string"
required = true
pattern = "^[^\\n]+$"
```

`type` is required and is one of:

| Type | Meaning |
|------|---------|
| `string` | text |
| `integer` | whole number |
| `number` | number |
| `boolean` | `true` or `false` |
| `enum` | one of `values` (required for `enum`) |
| `path` | a path relative to the piece file's directory; it must exist |
| `array` | a list of objects described by `items`, one level deep |

Optional keys are `required` (default `false`), `default`, `description`,
`min`, `max`, `pattern` (a Python regex matched with `fullmatch`), `min_items`,
and `values`. Options must match the type: `values` only on `enum`, `min` and
`max` only on `integer` and `number`, `pattern` only on `string` and `enum`,
`min_items` only on `array`. An array requires `items`, a table of fields in the
same language, and may not contain another array. A `default` must have the
field's type (and be one of `values` for an `enum`).

## Compatibility

`requires_facet` is evaluated with `packaging` against facet's own version,
prereleases included, and `protocol` must equal `1`. An incompatible package
stays in the registry but cannot be pulled or run: facet answers
`machine_cannot` (exit 5) with the hint `facet update, or upgrade facet`.

## Which registry facet uses

`FACET_REGISTRY` selects the registry directly when it holds an absolute path
(useful for development, tests, and the registry's own CI). Otherwise facet
uses the snapshot recorded by `facet update`: under `FACET_HOME` (default
`~/.facet`), the commit in `state/registry.json` selects
`registry/<commit>/`. A snapshot is complete when it holds `.complete`.

## `facet update`

```bash
facet update
facet update --ref feature/v0.1
facet update --ref <40-hex commit> --json
```

`facet update` resolves a reference to one exact commit and keeps that commit as
an immutable snapshot under `FACET_HOME`. Package runners are code that arrives
from the network, so the commit is always pinned and never changes silently.
Like every heavy verb, it takes the exclusive lock (see [home.md](home.md)).

- `REF` defaults to `$FACET_REGISTRY_REF`, else `main`, and may be a branch, a
  tag, or a 40-hex commit; `--ref` wins over the variable. During v0.1
  development the registry's work lives on `feature/v0.1`, so
  `FACET_REGISTRY_REF=feature/v0.1` is how facet is tested against it. The
  recorded `ref` is the reference used for that update, not a moving pointer:
  only the next update changes it.
- The reference is resolved with
  `GET https://api.github.com/repos/UniverLab/facetmedia-registry/commits/<REF>`.
  The fetch uses the GitHub API, whose anonymous limit is 60 requests per hour
  per IP; `GITHUB_TOKEN` is sent as `Authorization: Bearer` only when it is set,
  and then lifts that limit.
- `https://codeload.github.com/UniverLab/facetmedia-registry/tar.gz/<commit>` is
  downloaded to `<home>/tmp/registry-<commit>.tar.gz`, unpacked there, and the
  tarball is removed once the snapshot is installed or refused. Its one top
  directory is stripped. A member that is absolute, contains `..`, is a link, or
  is a device is refused. The snapshot is then loaded: one whose manifests do
  not validate is rejected and deleted.
- A snapshot that already holds `.complete` is never downloaded again; updating
  to the same commit only switches to it and records the new `ref`.
- `state/registry.json` records `commit`, `ref`, `fetched_at`, and `source`, and
  every later command reads that snapshot offline until the next update.
  Installed environments are not touched: a package whose manifest version
  changed is rebuilt by its next `pull` or `run`, and the output marks it
  `changed`.

The first command that needs the registry and finds no snapshot fetches it once,
printing `fetching the registry (first use)` to stderr. With no network and no
snapshot the error is `download_failed` (exit 8) with the hint `run facet update
when online`. When GitHub reports its rate limit (`403` with
`X-RateLimit-Remaining: 0`, or `429`), the message is `<url>: GitHub rate limit
reached`, followed by `(retry after <instant>)` when GitHub sent a reset, and
the hint is `set GITHUB_TOKEN to a GitHub token to lift the limit, or retry
after the reset` when no token was sent, `retry after the reset` when one was;
`details` then holds `url`, `rate_limited: true`, and `reset`, an ISO 8601 UTC
instant or `null`. Any other HTTP status keeps `HTTP <code>` and the hint
above. A failed update changes nothing: the previous snapshot stays in use.

With `--json`:

```json
{"contract": 1, "data": {"commit": "e475dbaca193af4d9c2e7b9f68f2c8a35fe2c6ca", "packages": [{"changed": true, "compatible": true, "name": "echo", "version": "0.1.0"}, {"changed": true, "compatible": false, "name": "future", "version": "0.1.0"}, {"changed": true, "compatible": true, "name": "termsy", "version": "0.1.0"}], "previous_commit": null, "ref": "feature/v0.1"}, "facet": "0.1.0b1", "ok": true}
```

`commit` is the pinned snapshot now in use and `ref` the reference it was
resolved from. `previous_commit` is the commit in use before this update, `null`
on the first. Each package record carries `name`, `version`, `compatible`
(whether this facet can run it), and `changed` (its manifest version differs
from the previous snapshot, so its next `pull` or `run` rebuilds its
environment).

Without `--json`:

```console
$ facet update --ref feature/v0.1
registry e475dbaca193af4d9c2e7b9f68f2c8a35fe2c6ca (ref feature/v0.1)
  echo 0.1.0 changed
  future 0.1.0 changed
  termsy 0.1.0 changed
```

`FACET_GITHUB_API` and `FACET_GITHUB_CODELOAD` replace the two GitHub base
URLs. They exist for the test suite, which serves both endpoints from
`127.0.0.1`, and for nothing else.

## `facet registry validate`

```bash
facet registry validate <dir>
facet registry validate <dir> --json
```

It loads the registry and reports **every** problem at once, not only the
first. Beyond the manifests it checks that each package directory has its
`pyproject.toml`, `uv.lock`, and `entry`; that each entry registers
`@runner.verb("doctor")` (a static parse of the entry file — no environment
is needed) and no longer registers `probe`, which is `rename probe to
doctor`; that every viewer named by a
capability has a `viewers/<viewer>/index.html`; that model ids are unique
across the registry; and that each package carries a valid
[`skill`](skill.md) — missing, misnamed, or misheaded skills are one
problem each. Exit `0` when the registry is clean and `2` otherwise.
Run it in the registry's CI before merging a package.

A clean registry with `--json`:

```json
{"contract": 1, "data": {"packages": ["echo", "future", "termsy"], "path": "tests/fixtures/registry", "valid": true}, "facet": "0.1.0b1", "ok": true}
```

Problems are printed to stderr as `file: key: problem`, and with `--json` the
same list is in `details.errors`:

```console
$ facet registry validate /tmp/broken-registry --json
packages/echo/package.toml: transport.entry: value
packages/echo/package.toml: typo_key: unknown
packages/echo/uv.lock: : missing
{"contract": 1, "error": {"code": "invalid_input", "details": {"errors": [{"file": "packages/echo/package.toml", "key": "transport.entry", "problem": "value"}, {"file": "packages/echo/package.toml", "key": "typo_key", "problem": "unknown"}, {"file": "packages/echo/uv.lock", "key": "", "problem": "missing"}]}, "hint": null, "message": "registry validation failed"}, "facet": "0.1.0b1", "ok": false}
```

`file` is relative to the registry root. `key` is the dotted key, empty when
the problem is the file itself. `problem` is one of:

| Problem | Meaning |
|---------|---------|
| `missing` | the file does not exist |
| `invalid_toml` | the file is not valid TOML |
| `required` | a required key is absent |
| `unknown` | the key is not part of the format |
| `type` | the value has the wrong type |
| `value` | the value is invalid (empty text, bad name, version, specifier, or pattern, wrong model source) |
| `duplicate` | a package name or model id is repeated |
| `min_items` | a required list is empty |

The `--json` envelope and exit codes are described in
[contract.md](contract.md): `invalid_input` exits `2`, and the envelope is the
only thing on stdout.
