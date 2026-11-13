---
title: Licences
description: How models with terms are accepted once, explicitly, before facet uses them.
order: 5
---

# Licences

Some models come with terms — a non-commercial licence, for example. Such
a model is used only after the person who governs the machine has accepted
its terms once, explicitly, with `facet license accept`. Only that verb
accepts; `pull` and `run` never accept on their own and instead fail with
the command to run. No environment variable accepts licences.

## Accepting

```bash
facet license accept            # at a terminal: pick from the pending ones
facet license accept <model>    # name one; facet asks in the terminal
facet license accept <model> --yes   # an agent the person directs
facet license list              # every model that requires acceptance
```

`facet license accept <model>` runs the flow below. For an unaccepted
model `termsy-weights` under CC-BY-NC-4.0, the listing reads:

    termsy-weights CC-BY-NC-4.0 not accepted

`accept` holds the facet lock exclusively, like `pull`, `rm` and `gc`;
`list` takes no lock. The model id must exist in the registry, otherwise
`invalid_input` (exit 2) lists the ids that require acceptance in
`details.known`. A model with `requires_acceptance = false` answers
`nothing to accept` and exits 0.

Accepting always prints to stderr first what is being accepted: the model
id, its package, its `spdx`, `commercial_use`, `terms_url`, and a line
saying that accepting makes the person who runs facet responsible for
complying with these terms.

The confirmation names the exact text to type, quoted, so the person never
has to guess the id. For `termsy-weights` it reads:

    Type "termsy-weights" to accept, or press Enter to cancel:

- The exact model id records the acceptance. Anything else records
  nothing and exits 2 with a message naming what was expected.
- Enter cancels: nothing is recorded, facet prints
  `cancelled; nothing was recorded`, and exits 0.
- Without `--yes`, a named model needs both stdin and stdout to be a
  terminal. Off a terminal it fails with `license_needs_person` (exit 10)
  and the hint `run it in a terminal, or pass --yes`.
- With `--yes`, the acceptance is recorded at once — no question is asked.
  This is the agent path: a person who directs an agent to pass `--yes` is
  the person accepting, and the same responsibility applies. The record
  says so with `method: "flag"`.

Accepting again replaces the earlier record for that model. A licence
change asks again: a record matches only while its `spdx` equals the
manifest's current `spdx`.

## Choosing from the pending ones

`facet license accept` with no model id is the wizard, for a person. It
runs when stdin and stdout are terminals and neither `--json` nor `--yes`
is given; in every other case the verb fails with `invalid_input` (exit 2)
naming the pending ids and the form `facet license accept <model-id>`.
`--yes` never accepts more than the one model named.

The wizard lists every model that needs acceptance and has none recorded,
whether or not its package is installed — `pull` demands acceptance before
downloading, so a pending model of a package that is not installed is
exactly the case a person must handle. Each numbered line shows the model
id, its package, whether that package is installed, its SPDX and whether
commercial use is allowed:

    1) omnivoice  omnivoice (not installed)  CC-BY-NC-4.0  commercial use not allowed

Pick one by its number, or `q` to quit. Any other answer prints
`enter a number from the list, or q` and asks again. Picking a model shows
its full terms block and then the confirmation above; after an acceptance,
a cancel or a mismatch it returns to the list, which is re-read on every
pass, so an accepted model no longer appears. When nothing is pending the
wizard says `no model needs acceptance` in one line. It exits 0 when
nothing is pending or the person quits.

## Backend-owned models

A model with `backend = true` in the manifest is served by the backend:
facet never fetches it and never hash-checks it. `pull` skips it and says
so on stderr:

```text
model omnivoice-v1 backend-owned, skipped
```

Its terms still apply. `accept` and `list` treat a backend-owned id
exactly like a downloadable one, and `run` requires acceptance for the
pieces that select it (see below) — a missing record fails with
`license_not_accepted` (exit 4) and the `facet license accept <model>`
command, even though there is nothing to download.

## Which models a run requires

`facet run` gates per piece, after the piece inputs are read. When the
capability declares a `model` input, the piece's value names the manifest
model id the run requires — exact id equality, nothing fuzzier — and only
that model needs acceptance. A piece selecting an ungated model runs with
no acceptance recorded. A capability with a `model` input never declares
`models`: the two never mix.

A capability without a `model` input may declare `models = [...]` in its
package manifest, naming the `[[models]]` ids it uses. The gate then
asks acceptance only for those models; an empty list means the capability
uses no manifest model and needs no acceptance at all. Without a
`models` declaration, a capability keeps the package-wide gate: every
gated model of the package needs acceptance. `facet describe` lists the
same models the gate would ask for.

## What the record keeps

```json
{"version": 1, "accepted": [{"model": "termsy-weights", "spdx": "CC-BY-NC-4.0", "terms_url": "https://…", "commercial_use": false, "by": "you", "at": "2026-10-23T01:02:03Z", "method": "interactive"}]}
```

The record lives at `~/.facet/state/licenses.json` (`FACET_HOME` when
set). It keeps three things about who accepted and when:

- `by` — the system login name; no e-mail or other identity is stored;
- `at` — the UTC instant of the acceptance;
- `method` — `"interactive"` (typed in the terminal) or `"flag"`
  (`--yes` passed by command line).

Acceptance happens through `license accept` alone, and until then
every gate (`pull`, `run`, `doctor`, `check`) names the exact accept
command.

## Listing

`facet license list` prints one line per registry model that requires
acceptance, each with a status. For an unaccepted model, a listing line
reads:

```text
termsy-weights CC-BY-NC-4.0 not accepted
```

Lists every registry model that requires acceptance, with a status per
model: required or not needed. `facet license list --json` reports the
same as data: `{"models": [...]}`. A registry whose models all need no
acceptance prints nothing — no news is the clean news.

The acceptance state of one capability's models is also visible up front:
`facet describe <capability>` reports per model its `spdx`,
`commercial_use`, `requires_acceptance` and whether it is already
`accepted` (see [commands.md](commands.md#facet-describe)).
