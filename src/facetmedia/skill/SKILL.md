---
name: facet
description: Make images, audio, voice, video, cad, text and presentations with facet's isolated packages.
---
# Facet

Facet makes things through isolated packages. Each package declares one
**facet** (its domain: `voice`, `audio`, `image`, `video`, `cad`, `text`,
`presentation`) and one or more capabilities named `<package>.<name>`.
A capability's **kind** is the asset it writes (`audio`, `text`, `cad`,
...); it picks the viewer. A package's **weight** is derived, never
declared: `light` runs anywhere, `cpu-heavy` needs the machine to itself,
`gpu` needs a GPU.

## The loop

```bash
facet list --available                  # packages and capabilities, by facet, with weights
facet describe <capability> --json      # inputs, example piece, licences, download size
facet doctor <capability>               # can this machine run it
facet license accept <model> --yes      # when the doctor asks for it
facet run <capability> <piece> --json   # make the asset; the record judges it
facet preview <asset> --json            # read a failed asset before fixing it
facet check <asset>                     # re-judge one asset after a fix
```

## Discover

Start with `facet list --available`: packages grouped by facet with their
weight. Filter with `facet list --available --facet voice`. With `--json`
each package row adds `facet` and `weight`. `facet describe <capability>
--json` adds `facet` beside `kind`, `weight`, and `skill` (the command that
prints that package's how-to).

## Run

`facet describe` prints `piece_example`: a minimal valid piece, the starting
point for a piece file. `facet run` pulls the package when needed, runs it
isolated, and writes the asset plus its record. Read the record, not only
the asset.

Every verb with `--json` prints one envelope on stdout:

```json
{"contract": 1, "ok": true, "facet": "0.1.0.dev0", "data": {...}}
{"contract": 1, "ok": false, "facet": "0.1.0.dev0", "error": {"code": "invalid_input", "message": "...", "details": {}, "hint": null}}
```

Branch on the exit code, not the prose:

| Exit | Code | When |
|------|------|------|
| 0 | — | success |
| 1 | `internal` | unexpected error; the traceback is in the run log |
| 2 | `invalid_input` | bad arguments, an invalid piece, an unknown capability |
| 3 | `verification_failed` | the asset was made but its check failed |
| 4 | `license_not_accepted` | a model needs acceptance first |
| 5 | `machine_cannot` | RAM, disk, platform or a doctor check fails |
| 6 | `lock_busy` | the lock is held and `--no-wait` was given |
| 7 | `not_installed` | the package is not installed |
| 8 | `download_failed` | a download failed or its sha256 did not match |
| 9 | `runner_failed` | the package runner crashed or broke its protocol |
| 10 | `license_needs_person` | `license accept` without a terminal |

## Fix

When `verify` fails, run `facet preview --json`: it names the field that
broke and the viewer that shows it. Fix the piece, run again, then `facet
check <asset>` to re-judge. `facet check` never makes anything; it only
re-verifies.

## Share

Heavy packages wait for each other: only one exclusive package runs at a
time. `facet run` waits for the lock; pass `--no-wait` to fail at once with
`lock_busy` instead. Light verbs (`list`, `describe`, `skill`, `preview`)
never take the lock and work offline from the registry snapshot in use.

## Each package's how-to

`facet skill <package>` prints that package's skill: when to use it, one
complete piece per capability, what `verify` measures, and the pitfalls.
Start there before writing a piece. `facet license accept` and `facet pull`
are per model and per package; the skill's `## On this machine` section
says what is true here right now.
