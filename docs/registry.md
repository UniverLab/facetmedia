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
viewers/<viewer>/index.html          # person-facing viewers
```

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
```

These keys are required, and the text values may not be empty. Unknown keys
are an error at every level, named by their dotted key.

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

- `REF` defaults to `$FACET_REGISTRY_REF`, else `main`, and may be a branch, a
  tag, or a 40-hex commit; `--ref` wins over the variable. During v0.1
  development the registry's work lives on `feature/v0.1`, so
  `FACET_REGISTRY_REF=feature/v0.1` is how facet is tested against it. The
  snapshot in use keeps the ref it was fetched with until the next update.
- The reference is resolved with
  `GET https://api.github.com/repos/UniverLab/facetmedia-registry/commits/<REF>`.
  Set `GITHUB_TOKEN` to lift the anonymous rate limit; facet sends it as
  `Authorization: Bearer` only when it is set.
- `https://codeload.github.com/UniverLab/facetmedia-registry/tar.gz/<commit>` is
  downloaded into `<home>/tmp/` and unpacked there, with its one top directory
  stripped. A member that is absolute, contains `..`, is a link, or is a device
  is refused. The snapshot is then loaded: one whose manifests do not validate
  is rejected and deleted.
- A snapshot that already holds `.complete` is never downloaded again; updating
  to the same commit only switches to it.
- `state/registry.json` records `commit`, `ref`, `fetched_at`, and `source`, and
  every later command reads that snapshot offline until the next update.
  Installed environments are not touched: a package whose manifest version
  changed is rebuilt by its next `pull` or `run`, and the output marks it
  `changed`.

The first command that needs the registry and finds no snapshot fetches it once,
printing `fetching the registry (first use)` to stderr. With no network and no
snapshot the error is `download_failed` (exit 8) with the hint `run facet update
when online`. A failed update changes nothing: the previous snapshot stays in
use.

With `--json`:

```json
{"contract": 1, "data": {"commit": "e475dbaca193af4d9c2e7b9f68f2c8a35fe2c6ca", "packages": [{"changed": true, "compatible": true, "name": "echo", "version": "0.1.0"}, {"changed": true, "compatible": false, "name": "future", "version": "0.1.0"}, {"changed": true, "compatible": true, "name": "termsy", "version": "0.1.0"}], "previous_commit": null, "ref": "feature/v0.1"}, "facet": "0.1.0.dev0", "ok": true}
```

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
`pyproject.toml`, `uv.lock`, and `entry`; that every viewer named by a
capability has a `viewers/<viewer>/index.html`; and that model ids are unique
across the registry. Exit `0` when the registry is clean and `2` otherwise.
Run it in the registry's CI before merging a package.

A clean registry with `--json`:

```json
{"contract": 1, "data": {"packages": ["echo", "future", "termsy"], "path": "tests/fixtures/registry", "valid": true}, "facet": "0.1.0.dev0", "ok": true}
```

Problems are printed to stderr as `file: key: problem`, and with `--json` the
same list is in `details.errors`:

```console
$ facet registry validate /tmp/broken-registry --json
packages/echo/package.toml: transport.entry: value
packages/echo/package.toml: typo_key: unknown
packages/echo/uv.lock: : missing
{"contract": 1, "error": {"code": "invalid_input", "details": {"errors": [{"file": "packages/echo/package.toml", "key": "transport.entry", "problem": "value"}, {"file": "packages/echo/package.toml", "key": "typo_key", "problem": "unknown"}, {"file": "packages/echo/uv.lock", "key": "", "problem": "missing"}]}, "hint": null, "message": "registry validation failed"}, "facet": "0.1.0.dev0", "ok": false}
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
