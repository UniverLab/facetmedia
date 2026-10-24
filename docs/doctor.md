# `facet doctor`

`facet doctor` is about the tool: facet itself, each package's tool chain,
and this machine's readiness. It reports — it never downloads and never
repairs: every failing check comes with a `fix` to run. Questions about an
asset's bytes are [`facet check`](check.md)'s.

```bash
facet doctor                       # the facet section, then every installed package
facet doctor kokoro                # one package, every capability
facet doctor kokoro.speech         # that capability's readiness plus its package's doctor
facet doctor --quick               # only the quick depth of the package doctors
facet doctor --json                # one JSON document on stdout
```

Doctor takes the [facet lock](home.md#the-lock) shared and honours
`--no-wait` and `--wait-timeout`.

## The facet section

Nine checks about facet and this machine, each `{"ok", "detail", "fix"}`:

- `python`: the interpreter version;
- `uv`: uv imports, its binary runs, and reports its version;
- `home`: `~/.facet` exists, is writable, and the platform is POSIX
  (`FACET_HOME` overrides the home);
- `state`: every state file under `state/` parses;
- `lock`: the lock is free, or held by a live process, which is named; a
  `lock.json` whose pid is gone is reported as stale with the fix
  `remove <home>/state/lock.json` — reported, never removed, because the
  kernel `flock` is the truth;
- `registry`: the checkout in use is at the pinned commit and passes
  `facet registry validate` (fix: `run facet update`, or fix the
  `FACET_REGISTRY` checkout);
- `disk`: free GB at the home;
- `orphans`: environments or models no installed package references, with
  the fix `facet gc`;
- `gpu`: what `nvidia-smi` reports, or `none` — informative only.

## Readiness

For a package target the doctor reports the same readiness checks
`facet check` used to, each with a `fix`:

- `compatible`: the package's `requires_facet` and `protocol` match this
  facet (fix: `facet update, or upgrade facet`);
- `platform`: `sys.platform` is in `requirements.platforms`;
- `ram`: total and available GB from `/proc/meminfo` against `ram_gb`;
- `disk`: free GB at the home against what `pull` would still need;
- `gpu`: what `nvidia-smi` reports, or `none` — informative only, unless
  the manifest sets `gpu = true`, in which case its absence is
  `machine_cannot`;
- `host_libs`: each soname in `requirements.host_libs` loads with
  `ctypes.CDLL` (fix: the manifest's `host_hint`);
- `installed`: whether `<env>/.complete` matches the lock (fix:
  `facet pull <package>`);
- `licenses`: per model needing acceptance, accepted or not (fix:
  `facet license accept <model>`);
- `models`: present with the expected size, or missing (fix:
  `facet pull <package>`).

`can_run` (a capability target) is true when every readiness check but
`installed`/`models` passes *and* the package doctor's checks pass;
`needs_pull` is true when the environment is not installed or a model is
missing. For a capability target the data also carries `capability`.

## The package doctor: two depths

The runner protocol's `doctor` verb answers with
`{"checks": [{"name", "ok", "detail", "fix"}]}`, at the depth the request
names in `"depth"`. Without `--quick` the doctor runs `full`; `--quick` runs
only `quick`:

- **`quick`** must finish in about 10 s and writes nothing: imports, files,
  versions. It is what `probe` did. `--quick` runs only this depth.
- **`full`** may take up to 30 s and may write a throwaway output in the
  temporary directory — one word synthesised, one box exported — which facet
  removes afterwards.

facet enforces the timeout per depth (10 s / 30 s) and reports a timeout as a
failed check. The run gate is the one exception: there a quick doctor that
merely timed out becomes a warning (stderr, the run log and `data.warnings`)
and the run proceeds, while `facet doctor` and `facet pull` still report it as
a failure. A runner that does not register `doctor` yields `{"ok": false,
"detail": "this package has no doctor", "fix": "report it to the package's
maintainer"}` (the SDK writes it). An uninstalled package is skipped with an
`ok` check. `pull` and `run` run the same readiness checks plus this doctor
at `quick` depth before doing anything, and refuse exactly as the doctor
reports (`machine_cannot`, or `license_not_accepted` with the accept
command).

### Writing a package's doctor with the SDK

```python
from facet_runner_sdk import Request, Runner

runner = Runner()


@runner.verb("doctor")
def doctor(req: Request) -> dict:
    checks = [{"name": "imports", "ok": True, "detail": "tool imports", "fix": ""}]
    if req.depth == "full":
        # may run the tool once on a throwaway output in req.out_dir
        ...
    return {"checks": checks}


runner.main()
```

`req.depth` is `"quick"` or `"full"`. At quick depth write nothing; at full
depth anything you write goes into `req.out_dir`, which facet deletes. Every
check needs `name`, `ok`, `detail` and `fix`, and the list must not be
empty. `facet registry validate` refuses a runner that does not register
`@runner.verb("doctor")`, or one that still registers `probe`.

## `--json`

One envelope; `data` is:

```json
{"healthy": true, "facet": {"python": {"ok": true, "detail": "python 3.12.1", "fix": ""}, "...": {}}, "packages": {"echo": {"installed": true, "readiness": {"compatible": {"ok": true, "detail": "...", "fix": ""}, "...": {}}, "doctor": [{"name": "imports", "ok": true, "detail": "...", "fix": ""}]}}}
```

A capability target adds `capability`, `can_run` and `needs_pull`.

## Exit codes

`0` when healthy. Otherwise the first failing class, in this order:

| Order | Class | Exit | When |
|-------|-------|------|------|
| 1 | `machine_cannot` | 5 | a facet check, a readiness check other than `licenses`/`installed`/`models`, or a package doctor check failed |
| 2 | `license_not_accepted` | 4 | a model's terms are not accepted |
| 3 | `not_installed` | 7 | the target's package is not installed |
| 4 | `runner_failed` | 9 | a doctor verb crashed or broke the protocol |

`installed`/`models` failing with no target pick no class: they are what
`needs_pull` reports. With a target — a package name or a capability id —
naming an uninstalled package, `can_run` can still be true, but the exit is
row 3.

`invalid_input` (2) is an unknown target; a lock that never comes free is
`lock_busy` (6).

## Human output

One line per check — `✓` or `✗` with the name and the detail — and an
indented `→ fix` on failure:

```
healthy
facet:
  ✓ python: python 3.12.1
  ✗ orphans: 1 orphaned: /home/you/.facet/envs/ghost/1.0.0
    → fix: facet gc
echo (installed):
  ✓ compatible: requires_facet >=0.1.0b1,<0.2 satisfied by facet 0.1.0b1, protocol 1
  ✓ imports: echo.text echo runner imports (full)
```
