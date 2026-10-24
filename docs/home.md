# The facet home

Everything facet installs or remembers lives under one directory, `~/.facet`.
Nothing goes to the system. Set `FACET_HOME` to move it:

```bash
export FACET_HOME=/data/facet
```

The home is created on first use, with mode `0700` on the root:

```
~/.facet/
├── cache/uv/            # uv's cache (UV_CACHE_DIR)
├── envs/                # one environment per package version
├── logs/                # one debug log per invocation
├── models/
│   ├── direct/          # models with a url source
│   └── hf/              # Hugging Face cache (HF_HOME)
├── python/              # uv-managed Python interpreters
├── registry/            # extracted registry snapshots (one dir per commit)
├── state/               # registry.json, leftovers.json, licenses.json, lock files
└── tmp/                 # scratch, same filesystem, for atomic renames
```

The uv cache, the environments and the models deliberately share one
filesystem with `tmp/`, so uv's hardlinks work and state-file renames are
atomic. Facet refuses to finish creating a home whose directories land on
different filesystems.

## Logs

Every invocation writes a debug log (including tracebacks) to

```
$FACET_HOME/logs/<YYYY-MM-DD>/<run-id>.log
```

where `<run-id>` is a UTC timestamp plus 6 random hex characters, e.g.
`2026-10-23/20261023T173644Z-3fa91c.log`. When an unexpected error occurs,
the `internal` failure names that file in its message and in `details.log`
(see [the --json contract](contract.md)). State files are written the same
way a crash-safe install would want: through `tmp/` and an atomic rename, so
a machine that powers off mid-write keeps the previous version intact.

## The lock

Heavy work takes `state/lock` exclusively, so only one heavy job runs at a
time: `update`, `pull`, `rm`, `gc`, `license accept`, plus `run` and the asset
re-verification of `check` when the package manifest sets
`requirements.exclusive = true` (the default). Light runs take the lock shared
and overlap; `list`, `describe`, `preview` and a machine-only `check` take no
lock at all.

Every verb that locks accepts two flags, except the asset form of `check`,
which always waits with the defaults:

```bash
facet pull --no-wait              # fail at once instead of waiting
facet run  --wait-timeout 120     # give up after SECONDS (default 3600)
```

On failure the verb exits `lock_busy` (6) and names the holder. While waiting,
facet prints one line to stderr naming it too:

```
waiting for the facet lock, held by pid 4711 since 2026-10-23T173644Z: installing kokoro
```

The holder information comes from `state/lock.json`, which only an exclusive
holder writes (and deletes before releasing). The lock itself is a kernel
`flock`, so a process that crashes never leaves it stuck; a `lock.json` whose
pid is no longer alive is reported as `stale info` and ignored — the `flock`
is the truth.

## Where facet refuses to run

- A `FACET_HOME` under a mounted Windows drive (`/mnt/c/...`) is refused with
  `machine_cannot` (exit 5) and the hint `keep FACET_HOME on the Linux
  filesystem`: hardlinks and permissions break on drvfs and it is much
  slower. Keep the home on the Linux filesystem (WSL is fine).
- Native Windows is not supported in v0.1; use WSL. Every command answers
  `machine_cannot` (exit 5) with `native Windows is not supported in v0.1;
  use WSL`.
