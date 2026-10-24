# `facet check`

`facet check <asset-or-record>` is content only: it re-verifies an asset
facet made earlier against its record. Questions about a machine or a tool
are [`facet doctor`](doctor.md)'s — a `<capability-id>` first argument is
`invalid_input` with the hint `facet doctor <capability>`.

```bash
facet check out/echo.txt            # does this asset still pass?
facet check out/echo.txt.facet.json # the record names the same asset
facet check out/echo.txt --json     # one JSON document on stdout
```

## Re-verification

Facet reads the record beside the asset (or the record file itself; the
capability is read from the record, so there is no capability argument),
fails with `verification_failed` (exit 3) when the bytes changed, then calls
the runner's `verify` verb with the asset and the record's `params`, and
reports the fresh metrics next to the recorded ones. The record file is not
modified.

Like `run`, the re-verification holds the [facet lock](home.md#the-lock):
shared or exclusive following the package manifest, so it overlaps with light
runs and waits for heavy work. It waits with the defaults. A first argument
that is not an existing file is `invalid_input` (exit 2); a record whose
capability is not in the registry is `invalid_input`, and one whose package
is not installed is `not_installed` (exit 7) with `facet pull <package>`.

## `--json` data

```json
{"capability": "echo.text", "asset": "/tmp/out/echo.txt", "passed": true, "recorded": {"status": "passed", "metrics": {"match": 1}}, "fresh": {"passed": true, "metrics": {"match": 1}, "thresholds": {}, "verifier": "echo-content"}}
```

The exit code is `0` when the fresh verdict passes and `3` when it does not;
the envelope stays `ok: true` either way, because the check ran.
