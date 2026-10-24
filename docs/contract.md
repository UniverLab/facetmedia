# The `--json` contract

Add `--json` to any `facet` command for machine-readable output:

```bash
facet list --json
```

With `--json`, stdout carries exactly one JSON document: an envelope with
the contract version. Progress goes to stderr, and every invocation writes a
debug log under `~/.facet/logs/<YYYY-MM-DD>/`, so stdout is safe to parse.

## Success

```json
{"contract": 1, "data": {"packages": [], "registry": {"commit": "9f2c1ab", "ref": "main"}}, "facet": "0.1.0b1", "ok": true}
```

`contract` is the contract version (currently `1`); it changes only on a
breaking change to a `--json` output. `facet` is the facet version.
`data` holds the verb's result.

## Failure

```json
{"contract": 1, "error": {"code": "invalid_input", "details": {}, "hint": null, "message": "argument VERB: invalid choice: 'nope' (choose from update, pull, list, describe, skill, doctor, check, run, preview, rm, gc, license, registry)"}, "facet": "0.1.0b1", "ok": false}
```

Without `--json`, a failure prints its message (and hint, when there is
one) to stderr instead of JSON.

## Exit codes

Branch on the exit code without parsing prose:

| Code | Exit | When |
|------|------|------|
| (success) | 0 | |
| `internal` | 1 | unexpected error; the traceback goes to the run log under `~/.facet/logs/<YYYY-MM-DD>/`, and `message`/`details.log` name the file |
| `invalid_input` | 2 | bad arguments, a piece file that fails validation, an unknown capability, a registry checkout that fails `facet registry validate`, a `run` asset that would overwrite one of the run's inputs |
| `verification_failed` | 3 | the asset was made but its check did not pass |
| `license_not_accepted` | 4 | a model needs a person's acceptance that is not recorded |
| `machine_cannot` | 5 | the machine or facet's own health does not meet the requirements (RAM, disk, platform, a failing `facet doctor` check) |
| `lock_busy` | 6 | the lock is held and `--no-wait` was given, or the wait timed out |
| `not_installed` | 7 | the package is not installed: `run --no-pull`, re-verifying an asset of it, or a `facet doctor` target |
| `download_failed` | 8 | a model, environment, or registry download failed, or its sha256 did not match |
| `runner_failed` | 9 | the package runner crashed or broke the runner protocol |
| `license_needs_person` | 10 | `license accept` was run without an interactive terminal |

Bad arguments exit 2 and, with `--json`, still print an `invalid_input`
envelope on stdout.

A registry checkout that fails `facet registry validate` reports every
problem at once: `details.errors` lists `{"file", "key", "problem"}` for
each one, and the same lines go to stderr.
