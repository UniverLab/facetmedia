# `facet pull`

`facet pull <package>` installs a registry package so that it runs the same on
any Linux/WSL machine: a uv-managed Python, the exact dependency set of the
package's `uv.lock`, and its models downloaded once into `~/.facet/models` and
checked by sha256. Nothing is installed globally. A pull killed at any point is
repaired by the next pull; there is no repair command.

```bash
facet pull kokoro                   # install, waiting for the facet lock
facet pull kokoro --json            # one JSON document on stdout
facet pull kokoro --no-wait         # fail at once instead of waiting
facet pull kokoro --wait-timeout 120
facet pull kokoro --verify-models   # recompute sha256 of models already present
```

`pull` holds the [facet lock](home.md#the-lock) exclusively, like `update`.

## What it checks first

Nothing is written or downloaded until every check passes. This is the same
gate [`facet doctor`](doctor.md) reports, in this order — plus the package's
`doctor` verb at `quick` depth when the environment is already installed —
and each refusal is `machine_cannot` (exit 5), except the licence check,
which has its own error:

- the package is `compatible` with this facet version, with the hint
  `facet update, or upgrade facet`;
- the platform is in `requirements.platforms`;
- total RAM (`/proc/meminfo` `MemTotal`) meets `requirements.ram_gb`;
- free disk at the home meets `requirements.disk_gb` × 1.1, minus the bytes
  already installed under `envs/` and `models/` — the message carries both
  numbers;
- a GPU answers `nvidia-smi` when `requirements.gpu` says one is required (no
  v0.1 package does);
- every soname in `requirements.host_libs` loads with `ctypes.CDLL` (facet
  never installs host libraries; the manifest's `host_hint` is the hint);
- every model with `requires_acceptance = true` has an acceptance record in
  `state/licenses.json` for its `id` and `spdx`, else `license_not_accepted`
  (exit 4) lists each model, its licence, its `terms_url` and the command to
  run, `facet license accept <id>`. Nothing is downloaded in that case.

An unknown package is `invalid_input` (exit 2) and `details.known` lists the
package names in the registry. Once every check passes, the plan goes to
stderr: the Python version, the environment path, each model with its size and
whether it is already present, and the total to download.

## What it builds

The environment lives at `~/.facet/envs/<package>/<version>/`. When its
`.complete` marker holds the package's `uv.lock` sha256, the environment is
up to date and is skipped. Otherwise facet deletes the directory (an
interrupted or outdated build), copies the package from the registry snapshot,
and runs facet's own uv binary:

```text
uv sync --frozen --no-install-project --no-dev --python <engine.python> --project <env>/package
```

with `UV_PYTHON_INSTALL_DIR` under the home, so the managed interpreter is
downloaded into `~/.facet/python` and nothing goes to the system. uv's output
streams to stderr and to the run log; a non-zero exit is `download_failed`
(exit 8) carrying the last 40 lines. facet's runner SDK is copied into the
environment as `facet_runner_sdk.py` (see
[run.md](run.md#the-runner-protocol)), so a package runner only writes tool
code.

Models are stored under `~/.facet/models/direct/`. A `url` model (either
`https://` or `file://`) streams through `tmp/<model-id>.<random>.part` while
hashing; a sha256 mismatch deletes the part file and fails with
`download_failed` naming both hashes. An `hf_repo` model lists the pinned
revision through the Hugging Face API and fetches each file into its local
directory (`.gitattributes` and `README.md` are skipped; LFS files are
verified by their sha256, the rest by size). Network errors are retried three
times with 2 s / 8 s backoff, and a failed attempt leaves no part file. A
model already present with the expected size is skipped, its sha256
recomputed only with `--verify-models`. Requests carry only
`User-Agent: facetmedia/<version>`.

`.complete` is written last, atomically; its presence is the only meaning of
"installed". Without `--json`, a successful pull prints one line to stdout —
`<name> <version> installed at <env> (N bytes downloaded in T s)`, or
`already installed at <env>`.

## `--json` data

```json
{"package": "echo", "version": "0.1.0", "env": "/home/u/.facet/envs/echo/0.1.0", "python": "3.12", "already_installed": false, "downloaded_bytes": 0, "seconds": 3.9, "models": []}
```

`models` holds one record per model: `{"id", "path", "bytes", "status"}` with
`status` either `downloaded` or `present`. `already_installed` is true when the
environment's `.complete` marker already held the lock hash.
