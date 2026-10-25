# facet

facet installs creation tools — text to speech, transcription, audio
editing, CAD, ASCII art — each in its own pinned environment, and exposes
them to people and agents through one verifiable `--json` contract: a
command that makes an asset, a record that proves what made it and that the
asset passes its own check. It is an **internal preview**: the interface may
change between betas.

Creating something means choosing twice, on two axes:

- A **facet** is a domain of creation that groups packages. v0.1 ships four
  facets: `voice` with kokoro and whisper, `audio` with ffmpeg, `cad` with
  cad, and `text` with ascii. The full table of packages lives in the
  registry's [package list](https://github.com/UniverLab/facetmedia-registry#packages),
  and every package documents itself in its own
  `packages/<name>/README.md` there, plus a package skill that
  `facet skill <package>` prints.
- A **kind** is the kind of asset a capability writes, and it picks the
  preview and the viewer that show it. The facets and the kinds are
  independent: `whisper.transcribe` belongs to the `voice` facet and writes
  the `text` kind, because a transcription is text even though the tool is
  about voice.

The packages live in [UniverLab/facetmedia-registry](https://github.com/UniverLab/facetmedia-registry),
a repository facet reads offline from one pinned commit. Every page in this
guide:

- [Command reference](commands.md) — every verb with its flags, lock
  behaviour and exit codes.
- [The `--json` contract](contract.md) — the envelope, the error code table
  and the asset record.
- [Previews](preview.md) — how facet shows an asset to a person or to an agent.
- [Licences](licenses.md) — accepting model terms.
- [Adding a package](adding-a-package.md) — joining the registry as a
  package author.

## Install

```bash
uv tool install facetmedia
```

Linux and WSL only; native Windows is refused (use WSL) and a home on a
Windows drive such as `/mnt/c` is refused too.

## The first run

Pin the registry, then run the registry's own example — a short Spanish
narration with kokoro:

```bash
facet update --ref feature/v0.1
facet run kokoro.speak packages/kokoro/examples/laboratorio.toml --out out
```

`run` on a capability id is the shape of everything: it installs the
package if it is missing (a uv-managed Python, the exact `uv.lock` of that
package, its models downloaded once and hash-checked), validates the piece,
runs the tool inside that environment, verifies the asset, and writes the
record `<asset>.facet.json` beside it. Print the same run as one JSON
document with `--json`.

## Where things live

Everything facet installs or remembers lives under one directory, `~/.facet`;
nothing goes to the system. Set `FACET_HOME` to move it, `export
FACET_HOME=/data/facet`. The directory is created on first use:

```
~/.facet/
├── cache/uv/            # uv's cache (UV_CACHE_DIR)
├── envs/                # one environment per package version
├── logs/                # one debug log per invocation
├── models/
│   ├── direct/          # models with a url source
│   └── hf/              # Hugging Face cache (HF_HOME)
├── python/              # uv-managed Python interpreters
├── registry/            # extracted registry snapshots (one dir per commit)
├── state/               # registry.json, leftovers.json, licenses.json, lock files
└── tmp/                 # scratch, same filesystem, for atomic renames
```

Environments are persistent and safe to keep: heavy work takes a
`state/lock` exclusively, so only one heavy job runs at a time — `update`,
`pull`, `rm`, `gc`, `license accept` and a run under an exclusive manifest,
while light runs overlap. A crashed job leaves no stuck lock; the kernel
holds it. `facet rm` and `facet gc` are how disk comes back when you want it
back. Every invocation leaves a debug log under
`logs/<YYYY-MM-DD>/<run-id>.log`, and state files are written through
atomic renames, so a machine that powers off mid-write keeps its state.

## The registry and how it is pinned

facet never reads the registry online: it reads a snapshot it downloaded at
one exact commit.

```bash
facet update                        # pin main
facet update --ref feature/v0.1     # pin a branch during v0.1 development
facet update --ref <40-hex commit>  # pin one commit
```

- The reference defaults to `$FACET_REGISTRY_REF`, else `main`; `--ref`
  wins over the variable. So during v0.1 development,
  `export FACET_REGISTRY_REF=feature/v0.1` is how facet is tested against
  the registry's work branch.
- Package runners are code that arrives from the network, so the commit is
  always pinned and never moves silently: `state/registry.json` records the
  commit, and every later command reads that snapshot offline until the
  next `facet update`.
- The GitHub API resolve counts against the anonymous limit (60 requests an
  hour per IP); `GITHUB_TOKEN` is sent when set and lifts the limit.

For development and tests, `FACET_REGISTRY=/path/to/checkout` selects a
local checkout instead — facet validates it, as `facet registry validate`
does, and never downloads.

## How an agent starts

An agent learns facet from facet itself:

````bash
$ facet skill
---
name: facet
description: Make images, audio, voice, video, cad, text and presentations with facet's isolated packages.
---
# Facet

Facet makes things through isolated packages. Each package declares one
**facet** (its domain: `voice`, `audio`, `image`, `video`, `cad`, `text`,
`presentation`) and one or more capabilities named `<package>.<name>`.
A capability's **kind** is the asset it writes (…); it picks the viewer.
A package's **weight** is derived, never declared: `light` runs anywhere,
`cpu-heavy` needs the machine to itself, `gpu` needs a GPU.

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
````

`facet skill` never installs anything into an agent directory; skills live
in the registry snapshot and change with `facet update`. `facet skill
<package>` prints one package's skill.
