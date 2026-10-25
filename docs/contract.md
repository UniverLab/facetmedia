# The `--json` contract

Add `--json` to any `facet` command for machine-readable output:

```console
$ facet list --json
{"contract": 1, "data": {"packages": [], "registry": {"path": "…"}}, "facet": "0.1.0b1", "ok": true}
```

With `--json`, stdout carries exactly one JSON document: an **envelope**
with the contract version. Progress goes to stderr, and every invocation
writes a debug log under `~/.facet/logs/<YYYY-MM-DD>/`, so stdout is safe
to parse.

## The envelope

### Success

```json
{"contract": 1, "data": {"package": "ffmpeg", "pulled": true, "…": "…"}, "facet": "0.1.0b1", "ok": true}
```

`contract` is the contract version — the source constant `CONTRACT_VERSION`,
currently `1`; it changes only on a breaking change to a `--json` output.
Within a contract version, no envelope or `data` key is ever renamed once
released: a JSON consumer written against one version keeps parsing every
later run of it. `facet` is the facet version. `data` holds the verb's
result.

### Failure

```json
{"contract": 1, "error": {"code": "invalid_input", "details": {}, "hint": null, "message": "argument VERB: invalid choice: 'nope' (choose from update, pull, list, describe, skill, doctor, check, run, preview, rm, gc, license, registry)"}, "facet": "0.1.0b1", "ok": false}
```

`code` is from the table below, `message` is one line, `hint` is an
optional one-line fix, and `details` holds structured extras (an error
list, a log path, the known ids). Without `--json`, a failure prints its
message (and hint, when there is one) to stderr instead of JSON.

### Exit codes

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

## The asset record

Every asset facet makes gets a record beside it, `<asset>.facet.json`
(with JSON formatting when written; this shows the content, not the
layout). Record keys are never renamed within the record version either,
for the same reason. Annotations in `⟨…⟩`.

```jsonc
{
  "record": 1,                        // record format version — this document
  "id": "sha256:615f1b28…",           // "sha256:" + the asset's digest; the asset's name
  "name": "intro",                    // the asset's base name, as a piece named it
  "kind": "audio",                    // the capability's kind
  "mime": "audio/wav",
  "bytes": 12,                        // size of the asset
  "created": "2026-10-23T01:02:03Z",  // UTC, when the record was written
  "path": "intro.wav",                // relative to the record; no absolute paths
  "properties": {                     // the tool's own measurements of the asset
    "duration_s": 9.641,
    "lufs": -16.0,
    "peak_dbfs": -1.4,
    "sample_rate_hz": 24000
  },
  "provenance": {
    "capability": "kokoro.speak",     // what made it (<package>.<name>)
    "package": {
      "name": "kokoro",
      "version": "0.1.0",
      "lock_sha256": "c3aa9e26…"      // the package's uv.lock digest when it ran
    },
    "tool": { "name": "kokoro-onnx", "version": "0.6.1" },
    "models": [                       // models used, with what identifies their bytes
      {"id": "kokoro-82m-v1.0-onnx", "sha256": "b6e491fc…"},
      {"id": "faster-whisper-small", "revision": "536b0662…"}
    ],
    "params": {"language": "es", "speed": 1.0, "text": "Hola.", "voice": "ef_dora"},
    "params_sha256": "33531443…",     // digest of the params as given
    "inputs": [                       // path inputs, hashed before the run started
      {"path": "ref.png", "id": "sha256:8c7dd6f5…"}
    ],
    "seed": null,                     // number, when the tool is seeded
    "facet": "0.1.0b1",               // facet version that ran
    "registry": {"commit": "e475dbac…", "path": null}
                                      // the snapshot commit in use, or null path
                                      //   when FACET_REGISTRY pointed at a checkout
  },
  "verification": {                   // what verify measured and judged
    "status": "passed",               // "passed" | "failed" | "skipped" (--no-verify)
    "verifier": "faster-whisper-small@536b0662",
    "metrics": {"wer": 0.0},
    "thresholds": {"wer_max": 0.05}
  },
  "license": {                        // licences the asset was made under
    "commercial_use": true,           // false when any licence forbids it
    "accepted": [],                   // models accepted for this run
    "tools": [                        // the tool and its notable dependencies
      {"subject": "kokoro-onnx", "spdx": "MIT"},
      {"subject": "phonemizer", "spdx": "GPL-3.0-or-later"}
    ],
    "models": [
      {"id": "kokoro-82m-v1.0-onnx", "spdx": "Apache-2.0", "commercial_use": true}
    ]
  }
}
```

A non-commercial restriction travels with the asset this way: a consumer
reads `license.commercial_use` instead of re-resolving anything. `check`
re-verifies the asset against this record; `preview` embeds it as data.

Paths in a record are relative to the record's directory; no absolute path
appears anywhere in one.
