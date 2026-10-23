# `facet license`

Some models come with terms — a non-commercial licence, for example. Such a
model is used only after the person who governs the machine has accepted its
terms once, explicitly, with `facet license accept`. Only that verb accepts;
`pull` and `run` never accept on their own and instead fail with the command
to run. No environment variable accepts licences.

```bash
facet license accept termsy-weights          # ask in the terminal
facet license accept termsy-weights --yes    # an agent its governor directs
facet license list                           # every model that needs acceptance
facet license list --json
```

`accept` holds the [facet lock](home.md#the-lock) exclusively, like `pull`;
`list` takes no lock.

## `facet license accept <model-id>`

The model id must exist in the registry, else `invalid_input` (exit 2)
lists the ids that require acceptance in `details.known`. A model with
`requires_acceptance = false` answers `nothing to accept` and exits 0.

Accepting always prints to stderr what is being accepted first: the model
id, its package, `spdx`, `commercial_use`, `terms_url`, the line
`Commercial use is not allowed.` when commercial use is not allowed, and the
line `Accepting makes the person who runs facet responsible for complying
with these terms.`

- With `--yes`, the acceptance is recorded with `method: "flag"`.
- Without `--yes`, both stdin and stdout must be a terminal: facet asks
  `Type the model id to accept: ` and records with `method: "interactive"`
  only on an exact match (anything else exits 2 and records nothing). Off a
  terminal it fails with `license_needs_person` (exit 10) and the hint
  `run it in a terminal, or pass --yes`.

The record goes to `state/licenses.json`:

```json
{"version": 1, "accepted": [{"model": "termsy-weights", "spdx": "CC-BY-NC-4.0", "terms_url": "...", "commercial_use": false, "by": "<login name>", "at": "2026-10-23T01:02:03Z", "method": "interactive"}]}
```

`by` is the system login name; no e-mail or other identity is stored.
Accepting again replaces the earlier record for that model. A licence change
asks again: the record matches only while its `spdx` equals the manifest's
current `spdx`.

## `facet license list`

Lists every registry model that requires acceptance, with `accepted`, `by`,
`at`, and `method` (`--json` carries `{"models": [...]}`).

## `--json` data

`accept` returns its record; a moot accept returns
`{"model": "<id>", "accepted": false}`.
