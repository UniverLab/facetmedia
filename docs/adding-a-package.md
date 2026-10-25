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
packages/<name>/skill/SKILL.md    # the package skill
viewers/<viewer>/index.html       # person-facing viewers
schemas/preview/<kind>.schema.json  # one preview schema per asset kind
```

facet reads this layout offline from one pinned commit (see
[index.md](index.md#the-registry-and-how-it-is-pinned)). `facet registry
validate <dir>` loads every part and reports **every** problem at once;
run it before opening the pull request — the registry's CI does the same.

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
type = "python-env"   # the only engine in v0.1
python = "3.12"

[transport]
type = "cli"          # the only transport in v0.1
entry = "runner.py"   # relative to the package directory, no ".."
```

`invocation_template` may be omitted; the default is
`{{python}} -I {{entry}} {{verb}} --input {{input}} --result {{result}}`.

### `[requirements]`

```toml
[requirements]
gpu = false           # default false
ram_gb = 2            # default 0
disk_gb = 1.2         # default 0
exclusive = true      # default true: a run takes the facet lock exclusively
platforms = ["linux"] # default ["linux"]
host_libs = []        # shared libraries by soname, e.g. "libGL.so.1"
host_hint = ""        # how a person installs them
```

facet never installs host libraries; `host_hint` is what the readiness
check prints, and `exclusive = false` is what lets several light runs
overlap.

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

## Capabilities and their inputs

Each capability is `[capabilities.<name>]`, its id is
`<package>.<name>` (for example `textured.strip`), and `<name>` matches
`[a-z][a-z0-9-]*`:

```toml
[capabilities.strip]
summary = "One line, shown by facet list and describe."
kind = "text"   # audio | video | image | cad | presentation | text
```

`kind` is the asset the capability writes; it alone picks the preview
schema and the viewer.

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
valid on its types. Verify thresholds and the viewer:

```toml
[capabilities.strip.verify]
thresholds = { columns_max = 300 }

[capabilities.strip.preview]
viewer = "text"     # defaults to "generic"; the viewer must exist under viewers/
```

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
environment, so a package runner writes tool code and nothing else:

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
