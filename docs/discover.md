# `facet list`, `facet describe` and `facet skill`

These three verbs are the whole discovery surface: an agent learns what facet
can do by asking facet, not by importing a server. All three read the registry
snapshot and the home's install state only; none takes the
[facet lock](home.md#the-lock), none accepts the waiting flags, and
none downloads anything.

```bash
facet list                              # installed packages only
facet list --available                  # every package in the snapshot
facet list --available --facet voice    # one facet's packages
facet list --available --json           # the same, machine-readable
facet describe echo.text --json         # one capability's full contract
facet skill                             # how an agent learns each package
```

## `facet list`

Packages grouped by facet — `voice`, `audio`, `image`, `video`, `cad`,
`text`, `presentation`, empty facets omitted — each with its weight
(`light`, `cpu-heavy`, `gpu`; see [registry.md](registry.md#packagetoml)):

```bash
$ facet list --available
text:
  echo  light
    echo.text  text  Echo text back, repeated.
  future  cpu-heavy
    future.thing  text  A capability from the future.
  termsy  cpu-heavy  [installed]
    termsy.make  text  Make one termsy asset.
```

`--facet <name>` keeps one facet's packages. `--json` reports, per package:
`name`, `version`, `tool_name`, `provider`, `facet`, `weight`,
`compatible` (this facet satisfies the manifest's `requires_facet` and
`protocol`), `installed`, `env_bytes` (the environment's size on disk when
installed, 0 otherwise), `models_bytes` (the size of the models already
present), and `capabilities` with each id's `summary` and `kind`. The
top-level `registry` says which snapshot was read: `{"path": ...}` under
`FACET_REGISTRY`, otherwise the `commit` and `ref` that `facet update`
recorded. Output is sorted — packages by name, capabilities by id.

Without `--available` only installed packages are listed; incompatible
packages (`"compatible": false`) appear only with `--available`, so a client
can see what a newer facet would offer.

Without `--available`, nothing installed prints one line instead of nothing
at all — ``no packages installed; `facet list --available` shows the
registry's`` — and `facet list --facet <name>` with nothing installed in
that facet names it the same way. `--json` still reports an empty
`packages` list.

## `facet describe`

`facet describe <capability-id>` answers what a client needs to make
something: the input schema, a minimal example piece, the verification
thresholds, the requirements, the licences, and what is still to download.
The human form prints the summary, the package, the input fields with their
required/default state, and whether it is installed:

```bash
$ facet describe echo.text
summary: Echo text back, repeated.
package: echo 0.1.0 (Echo, facet)
inputs: repeat (integer, default 1), text (string, required)
installed: no
```

`--json` is the whole contract, inside the same
[`--json` envelope](contract.md) as every other verb:

```json
{"contract": 1, "data": {"capability": "echo.text", "contract": 1, "download_bytes": 0, "facet": "text", "inputs": {"$schema": "https://json-schema.org/draft/2020-12/schema", "additionalProperties": false, "description": "Echo text back, repeated.", "properties": {"repeat": {"default": 1, "maximum": 10.0, "minimum": 1.0, "type": "integer"}, "text": {"description": "Text to echo.", "type": "string"}}, "required": ["text"], "title": "echo.text", "type": "object"}, "installed": false, "kind": "text", "licenses": {"models": [], "tools": [{"spdx": "Apache-2.0", "subject": "echo"}]}, "package": {"name": "echo", "provider": "facet", "tool_name": "Echo", "version": "0.1.0"}, "piece_example": "[inputs]\nrepeat = 1\ntext = \"text\"\n", "preview": {"viewer": "generic"}, "requirements": {"disk_gb": 0.01, "exclusive": false, "gpu": false, "platforms": ["linux"], "ram_gb": 0.1}, "skill": "facet skill echo", "summary": "Echo text back, repeated.", "verify": {"thresholds": {}}, "weight": "light"}, "facet": "0.1.0b1", "ok": true}
```

- `inputs` is JSON Schema draft 2020-12, and `piece_example` is a minimal
  valid piece generated from the fields that are required or carry a default
  — the starting point for a piece file. Enum fields take their first value,
  numbers their minimum, booleans `false`, path fields `.` (the piece's own
  directory), and an array of objects becomes one `[[inputs.<field>]]` block
  per item its `min_items` requires.
- `licenses.models` carries, per model, its `spdx`, `commercial_use`,
  `requires_acceptance` and whether the acceptance is already `accepted`
  (see [docs/license.md](license.md)).
- `download_bytes` is what a `pull` would still download (the models still
  missing), and 0 when the package is installed.
- `contract` appears both in the envelope and in `data`, because clients
  cache `describe` output on its own.
- `facet` is the package's domain and `weight` its derived heaviness; `skill`
  is the command that prints that package's how-to (`facet skill <package>`).

An unknown capability id fails with `invalid_input` (exit 2) and lists the
known ids in `details.known`.

To learn a package, read [`facet skill`](skill.md): the core skill lists
every package by facet with its weight, and each package skill holds its
details.
