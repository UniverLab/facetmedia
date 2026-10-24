# `facet check`

`facet check` answers the two questions a client asks before and after
making something: "can this machine do it?" and "does this asset still
pass?". Both forms speak the same `--json` contract as everything else and
never download anything; the machine checks themselves take well under two
seconds.

```bash
facet check echo.text                 # can this machine run echo.text?
facet check echo.text out/echo.txt    # does this asset still pass?
facet check echo.text --json          # one JSON document on stdout
```

## Machine readiness: `facet check <capability-id>`

Without a second argument facet reports readiness and takes no lock. The
checks run in this order, each `{"ok", "detail"}` in the `--json` data:

- `compatible`: the package's `requires_facet` and `protocol` match this facet;
- `platform`: `sys.platform` is in `requirements.platforms`;
- `ram`: total and available GB from `/proc/meminfo` against `ram_gb`;
- `disk`: free GB at the home against what `pull` would still need;
- `gpu`: what `nvidia-smi` reports, or `none` — informative only, unless the
  manifest says `gpu = true`, in which case its absence fails;
- `host_libs`: each soname in `requirements.host_libs` loads with
  `ctypes.CDLL`; facet never installs host libraries;
- `installed`: whether `<env>/.complete` matches the lock;
- `licenses`: per model needing acceptance, accepted or not;
- `models`: present with the expected size, or missing;
- `probe`: when installed, the runner's `probe` verb result.

`can_run` is true when everything except `installed`/`models` passes (those
are fixed by `pull`, which `run` does by itself); `needs_pull` is true when
the environment is not installed or a model is missing. The exit code is 0
when `can_run`, otherwise 5 (`machine_cannot`) or 4
(`license_not_accepted`), whichever fails first in the order above.

`pull` and `run` run this same gate first and refuse with `machine_cannot`
and the manifest's hint (or `license_not_accepted` with the accept command).

## Re-verification: `facet check <capability-id> <asset-or-record>`

With an asset (or its `.facet.json` record) facet re-verifies an asset it did
not make: it reads the record, fails with `verification_failed` (exit 3) when
the bytes changed, then calls the runner's `verify` verb with the asset and
the record's `params`, and reports the fresh metrics next to the recorded
ones. The record file is not modified.

Like `run`, the re-verification holds the [facet lock](home.md#the-lock):
shared or exclusive following the package manifest, so it overlaps with light
runs and waits for heavy work.

## `--json` data

```json
{"capability": "echo.text", "can_run": true, "needs_pull": false, "checks": {"compatible": {"ok": true, "detail": "..."}, "platform": {"ok": true, "detail": "linux in [linux]"}, "ram": {"ok": true, "detail": "total 31.00 GB, available 24.10 GB, need 0.1 GB"}, "disk": {"ok": true, "detail": "..."}, "gpu": {"ok": true, "detail": "none"}, "host_libs": {"ok": true, "detail": "the package needs no host libraries"}, "installed": {"ok": true, "detail": "..."}, "licenses": {"ok": true, "detail": "no model requires acceptance"}, "models": {"ok": true, "detail": "the package has no models"}, "probe": {"ok": true, "detail": "echo.text echo runner is ready"}}}
```

```json
{"capability": "echo.text", "asset": "/tmp/out/echo.txt", "passed": true, "recorded": {"status": "passed", "metrics": {"match": 1}}, "fresh": {"passed": true, "metrics": {"match": 1}, "thresholds": {}, "verifier": "echo-content"}}
```
