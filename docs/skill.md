# `facet skill`

`facet skill` is how an agent learns a package: it prints the right skill
from the registry snapshot in use, plus what is true on this machine right
now. It is a light verb — no lock, offline, reads the snapshot like
[`describe`](discover.md) — and it never writes anything: **skills are
never installed** into `~/.claude/skills`, `~/.agents`, or any other agent
directory. A skill changes with `facet update` and disappears with
`facet rm`, the way a snapshot does.

```bash
facet skill                       # the core skill, then every package by facet
facet skill echo                  # echo's skill, then ## On this machine
facet skill echo.text             # same, for one capability id
facet skill echo --reference batching   # one reference file only
facet skill echo --json           # the same, machine-readable
facet skill echo --no-doctor      # skip the quick doctor in the machine section
```

With no argument the core skill prints first, then a generated
`## Packages in this registry` section: one line per package with its
weight, its capability ids and its skill `description`, grouped by facet,
plus the registry commit. With a package name or capability id that
package's `SKILL.md` prints, then `## On this machine`: installed or not
(and `facet pull <package>` with its download size when not); each model's
licence state (accepted, needs `facet license accept <model>`, or no
acceptance needed); and, when installed and unless `--no-doctor`, the quick
doctor's checks, one line each with its fix when failing. For a capability
id the section names that capability first.

When no registry can be loaded, the core skill still prints with one `Registry not available: ...` line under `## Packages in this registry`, and its `state` carries `registry: null` plus `registry_error` (`{code, message}`).

`--reference <ref>` prints that reference file only; an unknown ref fails
with `invalid_input` listing the known ones. An unknown package or
capability fails with `invalid_input` with the known names, as `describe`
does. With `--reference` the doctor is skipped.

`--json` carries the same split as data: `scope` (`core` or `package`),
`name`, `facet` (`null` for core), `weight` (`null` for core),
`description`, `body` (exactly what the human form prints), `references`
(the known names), and `state` — the generated section as data (`registry`
plus per-facet package rows for core; `installed`, `download_bytes`,
`models`, and `doctor` — `null` with `--no-doctor`, with `--reference`,
or when not installed — for a package).

## The skill format

A package is complete only with its skill. It lives in the registry,
versioned with the package:

```text
packages/<name>/skill/SKILL.md
packages/<name>/skill/references/<ref>.md   # optional, <ref> matches [a-z0-9][a-z0-9-]*
```

`SKILL.md` opens with a front matter block between two `---` lines holding
exactly two single-line keys: `name:` (equal to the package name) and
`description:` (one line, 20 to 200 characters, saying when an agent
should use the package). The parser is a small stdlib `key: value` split —
no YAML dependency.

The body has these level-2 headings, in this order:

```markdown
## When to use
## Pieces
## Judging the result
## Pitfalls
```

`## Pieces` holds one complete piece example per capability in a fenced
`toml` block; `## Judging the result` says what `verify` measures and
which preview fields to read to fix a failure. Every capability id of the
package appears in the body. `facet registry validate` rejects a package
whose skill is missing or breaks any of this, with one problem per
violation.

An agent starts from `facet skill`; a package skill holds that package's
details. The core skill (`facet skill` with no argument) teaches the loop
an agent follows: `facet list` → `facet describe` → `facet doctor` →
`facet license accept` → `facet run` → `facet preview` → `facet check`.
