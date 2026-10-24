# `facet list` and `facet describe`

These two verbs are the whole discovery surface: an agent learns what facet
can do by asking facet, not by importing a server. Both read the registry
snapshot and the home's install state only; neither takes the
[facet lock](home.md#the-lock), neither accepts the waiting flags, and
neither downloads anything.

```bash
facet list                              # installed packages only
facet list --available                  # every package in the snapshot
facet list --available --json           # the same, machine-readable
facet describe echo.text --json         # one capability's full contract
```

## `facet list`

One line per capability, `<id>  <kind>  <summary>`, with `  [installed]`
appended when the package is installed:

```bash
$ facet list --available
echo.text  text  Echo text back, repeated.
future.thing  text  A capability from the future.
termsy.make  text  Make one termsy asset.  [installed]
```

`--json` reports, per package: `name`, `version`, `tool_name`, `provider`,
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
{"contract": 1, "data": {"contract": 1, "capability": "echo.text", "summary": "Echo text back, repeated.", "kind": "text", "package": {"name": "echo", "version": "0.1.0", "tool_name": "Echo", "provider": "facet"}, "inputs": {"$schema": "https://json-schema.org/draft/2020-12/schema", "title": "echo.text", "description": "Echo text back, repeated.", "type": "object", "properties": {"text": {"type": "string", "description": "Text to echo."}, "repeat": {"type": "integer", "default": 1, "minimum": 1.0, "maximum": 10.0}}, "required": ["text"], "additionalProperties": false}, "piece_example": "[inputs]\nrepeat = 1\ntext = \"text\"\n", "verify": {"thresholds": {}}, "preview": {"viewer": "generic"}, "requirements": {"gpu": false, "ram_gb": 0.1, "disk_gb": 0.01, "exclusive": false, "platforms": ["linux"]}, "licenses": {"tools": [{"subject": "echo", "spdx": "Apache-2.0"}], "models": []}, "download_bytes": 0, "installed": false}, "facet": "0.1.0.dev0", "ok": true}
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

An unknown capability id fails with `invalid_input` (exit 2) and lists the
known ids in `details.known`.
