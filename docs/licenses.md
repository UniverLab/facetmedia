# Licences

Some models come with terms — a non-commercial licence, for example. Such
a model is used only after the person who governs the machine has accepted
its terms once, explicitly, with `facet license accept`. Only that verb
accepts; `pull` and `run` never accept on their own and instead fail with
the command to run. No environment variable accepts licences.

## Accepting

```bash
facet license accept <model>     # facet asks in the terminal
facet license accept <model> --yes   # an agent the person directs
facet license list               # every model that requires acceptance
```

Each form of `accept` runs the flow below. For an unaccepted model
`termsy-weights` under CC-BY-NC-4.0, the listing reads:

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

- Without `--yes`, both stdin and stdout must be a terminal. facet asks
  `Type the model id to accept: ` and records the acceptance only on an
  exact match; anything else exits 2 and records nothing. Off a terminal
  it fails with `license_needs_person` (exit 10) and the hint `run it in a
  terminal, or pass --yes`.
- With `--yes`, the acceptance is recorded at once — no question is asked.
  This is the agent path: a person who directs an agent to pass `--yes` is
  the person accepting, and the same responsibility applies. The record
  says so with `method: "flag"`.

Accepting again replaces the earlier record for that model. A licence
change asks again: a record matches only while its `spdx` equals the
manifest's current `spdx`.

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

`facet list` never shows an unaccepted model's terms next to a download
button; acceptance happens through `license accept` alone, and until then
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
