# `facet run`

`facet run <capability> <piece.toml | ->` turns a declarative piece into
verified assets with their records, in one command: it installs the package
if needed, validates the piece against the capability's schema, runs the
package's runner inside the package's own environment, verifies every asset,
and writes the records. The packages come from the registry snapshot in use;
the piece format is the lab's (`.toml` in, asset out, check).

```bash
facet run kokoro.speech speech.toml            # assets beside the piece file
facet run kokoro.speech speech.toml --out out  # assets into out/
echo '{"inputs": {"text": "hi"}}' | facet run echo.text - --out out --json
facet run echo.text piece.toml --no-verify     # skip the verify verb
facet run echo.text piece.toml --no-pull       # fail instead of pulling
facet run echo.text piece.toml --timeout 600   # kill the runner after 600 s
facet run echo.text piece.toml --ephemeral     # install, run, remove the environment
facet run echo.text piece.toml --no-wait       # fail at once instead of waiting
```

A piece is data — facet never evaluates code from one. The only code that
runs is the package's runner, behind the runner protocol (below), inside the
package's environment.

## The piece

A piece is a TOML table with one `inputs` map, validated against the
capability's declared inputs, which the registry declares per capability
([registry.md](registry.md#input-schema)). An unknown key, a missing required
input or a value outside its `min`/`max`/`enum`/`pattern` is
`invalid_input` (exit 2) listing every problem at once in `details.errors`.
`path` inputs are resolved relative to the piece file (with stdin: the
current directory) before the runner sees them. With `-` the piece is JSON
read from stdin, for clients that build pieces in memory.

## What happens, in order

1. The capability is resolved from the registry; an unknown id is
   `invalid_input` with the known ids.
2. Before anything is pulled, the machine gate of [`facet check`](check.md)
   runs (compatibility, platform, RAM, disk, GPU, host libraries and model
   acceptance); a refusal is `machine_cannot` (exit 5) or
   `license_not_accepted` (exit 4) for a model whose terms were not accepted
   with `facet license accept`.
3. If the package is not installed, it is pulled first (same lock rules and
   checks as [`facet pull`](pull.md), its plan printed to stderr and the run
   log before it starts) — unless `--no-pull`, which is `not_installed`
   (exit 7). With `--ephemeral` a package this run installed is removed again
   before facet exits — after a successful run, a failed one, or a piece it
   never got to run — while its models stay; a package that was already
   installed is left alone, and facet says so on stderr.
4. The piece is validated against the capability's inputs.
5. The [facet lock](home.md#the-lock) is taken: exclusive if the package's
   `requirements.exclusive` says so or `--ephemeral` is given, else shared.
   `--no-wait` and `--wait-timeout S` (default 3600) shape the wait, as for
   every verb that locks.
6. The runner is called through the file protocol: facet writes a request
   file, the package's runner (its own interpreter, its own process group)
   writes assets into `<out>/.facet-tmp-<run-id>/` and a result file, then —
   unless `--no-verify` — its `verify` verb judges those assets.
7. Each asset is moved onto `<out>/<file>` (`os.replace`; an existing file of
   the same name is replaced) and its record (`<asset>.facet.json`) is
   written beside it; then the temporary directory is removed.
8. Exit 0 when every asset passed (or verification was skipped), exit 3
   (`verification_failed`) when any failed. Either way the `--json` envelope
   is printed with every record; failed assets and their records are kept so
   an agent can inspect them.

A runner that exits non-zero, writes no or a malformed result file, or lists
an asset it did not produce is `runner_failed` (exit 9) with the last 40
lines of its output, the run-log path and the kept temporary directory. That
directory's absolute path is appended to `<home>/state/leftovers.json`, so
[`facet gc`](gc.md) removes it once it is no longer wanted. `--timeout S` and
an interrupt (SIGINT/SIGTERM) do the same: facet sends SIGTERM to the
runner's whole process group, then SIGKILL after 10 s, so no child keeps
memory or VRAM. Runner output is streamed to facet's stderr and the run log,
never to its stdout.

## `--json`

The data of a successful envelope (errors use the standard
[contract](contract.md) envelope):

```json
{
  "capability": "echo.text",
  "out_dir": "/path/to/out",
  "pulled": false,
  "downloaded_bytes": 0,
  "assets": [{"id": "sha256:...", "...": "the full record of each asset"}]
}
```

`pulled` says whether this run had to install the package first, and
`downloaded_bytes` how much that fetched. Each entry of `assets` is the
complete record written beside the asset: capability, package lock, tool,
models, parameters, seed, inputs, the verification verdict
(`passed`/`failed`, or `skipped` with `--no-verify`) and the licences it was
made under.

Without `--json`, each asset prints one line —
`<file> <name> <passed|failed|skipped>` — and then `out <out-dir>`.

## The runner protocol

The runner is a separate interpreter, invoked through the package manifest's
`transport.invocation_template` with `{{python}}` = the environment's
interpreter, `{{entry}}` = the runner script, `{{verb}}`, `{{input}}` and
`{{result}}`. facet writes a request file (`{"protocol": 1, "verb",
"capability", "inputs", "out_dir", "piece_dir", "models", "assets"}`, where
`models` maps each model id to its absolute path under
`~/.facet/models/direct`) and the runner writes a result file: for `run`, its
tool and one entry per produced asset (file, name, kind, mime, properties,
params, seed, inputs); for `verify`, one verdict per asset; `preview` and
`probe` serve `facet preview` and `facet check`. A `file` is one bare name
inside the temporary directory — never a path, and never the reserved
`.facet-` prefix. The runner's environment is facet's own plus `FACET_MODELS`
(that models directory) and an empty `CUDA_VISIBLE_DEVICES` when the manifest
says `gpu = false`. The `src/facetmedia/runner_sdk.py` module ships with
facet and is copied into every environment as `facet_runner_sdk`, so a
package runner only writes tool code:

```python
from facet_runner_sdk import Runner

runner = Runner()


@runner.verb("run")
def run(req) -> dict:
    ...
    return {"tool": {"name": "echo", "version": "0.1.0"}, "assets": [...]}


runner.main()
```

`req` carries the request: `inputs` (validated, defaults filled, paths
absolute), `out_dir`, `piece_dir`, `assets` (the files to judge, on `verify`
and `preview`) and `model_path(id)`. An unknown verb or an exception exits 1
with the reason on stderr, which facet turns into `runner_failed`.
