---
title: The --json contract
description: The JSON envelope every facet command prints, its error codes and exit statuses.
order: 3
---

# The `--json` contract

Add `--json` to any `facet` command for machine-readable output:

```console
$ facet list --json
{"contract": 1, "data": {"available": {"capabilities": "…", "facets": "…", "packages": "…"}, "packages": [], "registry": {"path": "…"}}, "facet": "0.1.0b2", "ok": true}
```

The keys above are the examples, not the whole `data`: `facet describe`
adds `i18n` (localized summaries keyed by language tag, `{}` when none),
`piece_example` (the capability's first bundled example piece as TOML
text, or a synthetic minimal piece when it ships none), `example_path`
(that example's path relative to the package directory, `null` when the
text is synthetic) and `secrets` (one `{name, description, required, set}`
row per credential the package declares — never a value), and `facet list
--available` adds a `facets` map with each facet's `label` and `i18n` when
`index.toml` declares them. Both are additive, so no consumer written
against this version needs to change.

## Credentials

Credentials live in one encrypted vault, `<home>/secrets/vault.enc`. The
encryption protects against reading the vault file, copying it to another
machine or user, and backups. It does not protect against a program running
as the same user on the same machine when there is no passphrase, since that
program can derive the same key. `facet secret passphrase` sets, changes or
removes the passphrase; without a terminal the passphrase comes from
`FACET_SECRETS_PASSPHRASE`, and a vault that cannot be opened fails with
`machine_cannot` and is never overwritten.

A stored secret never appears in any output: not in `data`, not in an
error's `message`, `hint` or `details`, not in a record, a preview or a log.
Every surface reports a secret's name, its one-line description and whether
it is set — never a value. `obtain` says where to get the value and
`produced_by: "auth"` marks a secret the package's `auth` verb mints; both
travel beside the description and are `null` unless the manifest declares
them. `facet secret list` answers
`{"secrets": [{"description": "…", "name": "app-password", "obtain": null, "package": "bluesky", "produced_by": null, "required": true, "set": true}]}`,
and `facet describe` carries the same rows under `data.secrets` for the
capability's package.

A `facet run` whose package declares a required secret that is not stored
fails with `machine_cannot` **before the runner starts**:
`error.message` names the missing secret or secrets, `error.hint` is the
exact `facet secret set <package> <name>` (or `facet auth <package>` when
the package provides that verb), and `error.details` carries
`{"package": "…", "missing": ["…"]}`. The runner of that package receives
its own set secrets as `FACET_SECRET_<NAME>` environment variables and no
other package's; any occurrence of one of those values in the runner's
stdout or stderr is replaced with the fixed mask `***` before facet keeps,
logs or shows it.

Every runner request carries `renewals_file`: the path of a per-call, empty
file (UTF-8, one `name=value` line per renewal) where a verb may record a
refreshed secret through the runner SDK. It also carries `package`: the
manifest `name` the runner's `secret()` uses to name the exact
`facet secret set <package> <name>` command when a secret is not set
(`secret()` raises `SecretNotSet`, a `KeyError` subclass, and a verb that
raises it or `ValueError` fails with one `error: <message>` line and no
traceback). After the verb ends, by success,
failure or exception, facet stores each renewed value the package declared
(the last line wins when a name is renewed more than once),
deletes the file and its directory in every case, and masks every renewed
value the same way as a stored one — including the output tail in an error's
`details.output` and the kept log. A name the package did not declare is
skipped and reported once as a warning: in `data.warnings` on success, or in
`error.details.warnings` on failure (a failure envelope carries no `data`).
If storing a renewed secret fails, the file is deleted anyway and the error
is `machine_cannot` with the message
`the renewed secret was lost; run facet auth`.

With `--json`, stdout carries exactly one JSON document: an **envelope**
with the contract version. Progress goes to stderr, and every invocation
writes a debug log under `~/.facet/logs/<YYYY-MM-DD>/`, so stdout is safe
to parse.

## The envelope

### Success

```json
{"contract": 1, "data": {"package": "ffmpeg", "pulled": true, "…": "…"}, "facet": "0.1.0b2", "ok": true}
```

`contract` is the contract version — the source constant `CONTRACT_VERSION`,
currently `1`; it changes only on a breaking change to a `--json` output.
Within a contract version, no envelope or `data` key is ever renamed once
released: a JSON consumer written against one version keeps parsing every
later run of it. `facet` is the facet version. `data` holds the verb's
result.

### Notices

A success envelope may carry an optional top-level `notices` list. The key is
present only when the list is not empty; a consumer that does not know it
keeps working.

```json
{"contract": 1, "data": {"…": "…"}, "facet": "0.1.0b2", "notices": [{"available": "0.2.0", "installed": "0.1.0", "kind": "update", "package": "comfy"}], "ok": true}
```

`kind: "update"` names an installed package whose version in the registry
snapshot is newer than the one on disk; `installed` and `available` are those
two versions. The notice never changes `data`, the envelope's `ok`, or the
exit code: `facet update` pins the registry, and `facet pull --package <name>`
installs the newer version.

### Failure

```json
{"contract": 1, "error": {"code": "invalid_input", "details": {}, "hint": null, "message": "argument VERB: invalid choice: 'nope' (choose from update, pull, list, describe, example, run, check, preview, doctor, auth, remove, clean, skill, license, config, secret, registry)"}, "facet": "0.1.0b2", "ok": false}
```

`code` is from the table below, `message` is one line, `hint` is an
optional fix, and `details` holds structured extras (an error list, a log
path, the known ids). A piece that fails validation carries one
`<field>: <rule>` (or `<field>: <problem>`) line per error in `hint`, so
that hint can be several lines; every other hint is one line. Without
`--json`, a failure prints its message (and hint, when there is one) to
stderr instead of JSON; at a terminal the message is prefixed `✗ ` and the
two lines are styled, while a pipe, a log file or `NO_COLOR` gets the bare
lines. A failing `pull` or `update` also repeats the last
20 lines of its run log on stderr, then a `log: <path>` line, with or
without `--json` and `--verbose`; stdout stays a single JSON document.

A `facet run` whose asset fails verification is `ok:false` **with** both
`data` (the records, `assets` and `out_dir`) and an `error` of code
`verification_failed`: `message` is `<k> of <n> assets failed verification:
<names>`, `details.assets` lists the failed asset paths, `hint` is
`facet check <first failed asset>`, and the exit code is 3. `facet example
--run` inherits the same envelope. So a reader of only `ok`, or only the exit
code, sees the failure.

An unhealthy `facet doctor` is the one envelope that reports `ok:false`
**with** `data` and no `error`: `data.healthy` is `false`, and the exit
code is the first failing class.

### An outward run: dry run, yes, and the receipt

A capability of kind `post` is **outward**: it sends a request out instead
of writing a file. The network may keep the post as a draft or private, and
the request cannot be undone and may cost money, so the envelope carries
four additions and nothing else changes for a file capability.

`facet run <piece> --dry-run` calls the runner's `dry-run` verb, which sends
nothing and writes nothing into the output folder. `data.dry_run` is `true`
and `data.preview` is what would be sent: one entry per post with its
`text`, `account`, `media`, `warnings` and the runner's `cost` estimate
(`null` when the network does not charge). `data.assets` is absent.

A real outward run needs `--yes`. At a terminal without it, facet shows the
dry-run preview and asks; anything but yes prints `nothing sent` and exits
`0`, as a declined `facet pull` does. With `--json` or without a terminal it
fails with `confirmation_required` (exit 11), `details.preview` carrying the
same preview. `--yes` is never implied by another flag or setting.
`data.sent` is `false` for a declined run.

Before any confirmation or runner call, a real outward run checks whether the
receipt it would write already exists in the output folder — the
**receipt-exists rule**. If it does, the run fails with `invalid_input`
(exit 2): `details.receipt` is the path, and the hint says the piece was
already sent and how to send it again on purpose (remove the receipt, or
choose another output folder with `--out`). A dry run in that situation still
prints its preview and adds a warning about the receipt. The rule applies to
outward capabilities only; other capabilities keep their behaviour.

A real outward run's envelope carries one entry in `data.assets` with
`kind: "post"`, and the receipt is that asset. `data.sent` is `true` for a
real run and absent for a dry run:

```jsonc
{
  "receipt": 1,                       // receipt format version
  "network": "bluesky",               // the network, as the runner named it
  "account": "lab.test",              // the account that sent it
  "state": "published",               // published | draft | private | scheduled | unknown
  "posts": [{"id": "at://…", "url": "https://…"}],
  "sources": [                        // the assets the post carried
    {"path": "card.png", "id": "sha256:…", "generated": false}
  ],
  "cost": {"amount": "0", "currency": "USD"},   // as the runner reported it; null when free
  "sent_at": "2026-11-17T19:44:03Z",  // ISO 8601, UTC
  "verified_at": "2026-11-17T19:44:03Z"         // null when no read-back happened
}
```

The receipt's file name comes from the piece, never from the run id: the
piece `talk.toml` writes `talk.post.json` beside its other outputs (a piece
read from stdin uses the capability's short name). `sources[].generated` is
derived from the source's own record: a non-empty `provenance.models` list
means a model generated it, and a source without a record is not generated.
`cost` is passed through without interpretation. The receipt is recorded like
any asset, with `verification.metrics` carrying the read-back's `state` and
`media` count.

A real outward run is never retried by facet. Once the runner reports that a
request left, facet keeps the receipt even if a later step fails: the receipt
then carries `state: "unknown"` and `verified_at: null`, and the error says
the post may exist and where to check (`details.urls`). That includes a `run`
verb that exits 0 but answers outside the protocol: the receipt facet writes
still exists, so the receipt-exists rule can refuse a second send, and it
says only `unknown` — the package's name as the network and the account the
dry run named, because the runner said nothing facet could read.

Verification of a `post` asset reads the post back from the network. It
passes only when the state read back equals the receipt's state and the media
count matches, so facet never reports `published` for a post the network
holds as a draft or private. `--no-verify` is refused for an outward
capability with `invalid_input` (exit 2).

### Exit codes

Branch on the exit code without parsing prose:

| Code | Exit | When |
|------|------|------|
| (success) | 0 | |
| `internal` | 1 | unexpected error; the traceback goes to the run log under `~/.facet/logs/<YYYY-MM-DD>/`, and `message`/`details.log` name the file |
| `invalid_input` | 2 | bad arguments, a piece file that fails validation, an unknown capability, a registry checkout that fails `facet registry validate`, a `run` asset that would overwrite one of the run's inputs, `--dry-run` or `--yes` on a capability that is not outward, `--no-verify` on one that is, and a real outward run whose receipt already exists |
| `verification_failed` | 3 | the asset was made but its check did not pass; for a `post`, the state the network holds is not the state the receipt reports |
| `license_not_accepted` | 4 | a model needs a person's acceptance that is not recorded |
| `machine_cannot` | 5 | the machine or facet's own health does not meet the requirements (RAM, disk, platform, a failing `facet doctor` check), or a `run` whose package needs a required secret that is not stored |
| `lock_busy` | 6 | the lock is held and `--no-wait` was given, or the wait timed out |
| `not_installed` | 7 | the package is not installed, or its files changed in the registry since it was installed: `run --no-pull`, re-verifying an asset of it, or a `facet doctor` target |
| `download_failed` | 8 | a model, environment, server or source, or registry download failed, or its sha256 did not match |
| `runner_failed` | 9 | the package runner crashed or broke the runner protocol |
| `license_needs_person` | 10 | `license accept` was run without an interactive terminal |
| `confirmation_required` | 11 | a pull installs more than one package and no interactive terminal can confirm it; a real outward `run` without `--yes` and without an interactive terminal; bare `facet auth`, `facet auth login` or `facet auth logout` with no interactive terminal to choose on; bare `facet secret set` with no interactive terminal to choose on. For the first two, rerun with `--yes` once the preview is what you meant to send; `details.preview` carries it. For auth, name the package: `facet auth <package>` or `facet auth logout <package>`; `details.packages` lists the registry's package names. For secret set, name the package and secret: `facet secret set <package> <name>` |
| — | 130 | interrupted with Ctrl+C; prints `cancelled`, and installs or records nothing that had not already completed |

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
  "kind": "audio",                    // the capability's kind; "post" for a receipt
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
    "facet": "0.1.0b2",               // facet version that ran
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

An input may be a file or a directory of files. A directory is one asset:
its id is a content hash of its tree — every regular file under it, in
relative-path order, each with its own sha256 — and empty subdirectories
do not change it. A record beside a directory asset (`<dir>.facet.json`
next to the tree) describes the tree the same way: `bytes` is the sum of
the file sizes, and `check` re-verifies it like any asset.

Paths in a record are relative to the record's directory; no absolute path
appears anywhere in one.

A parameter is treated as a path only when the filesystem confirms it is
one. Text that cannot be a path — a string too long for a file name, or
holding characters no path may have — stays text: the record keeps it as
given, and `check` re-verifies a record holding it normally, never
treating it as a missing or oversized file.
