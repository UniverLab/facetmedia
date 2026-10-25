# facetmedia

facet installs each creation tool (Kokoro, Whisper, FFmpeg, CadQuery, …)
in its own pinned uv environment, groups the tools by facet, and exposes
them to agents through one verifiable `--json` contract and on-demand
skills: a command that turns a data piece into an asset, a record that
proves what made it, and a check that the asset still passes.

> Internal preview: the interface may change between betas.

## Install

```bash
uv tool install facetmedia
```

Linux and WSL only. Everything facet installs or remembers lives under one
directory, `~/.facet` (override with `FACET_HOME`): environments, models,
the pinned registry snapshots, per-invocation logs and state. The user
guide is [docs/index.md](docs/index.md) — install, the facets and their
packages, the first run, and how an agent starts with `facet skill`.

## Commands

| Command | Purpose |
|---------|---------|
| `facet update` | download the registry at a pinned commit |
| `facet pull` | install a tool into its own pinned environment |
| `facet list` | list installed and available tools |
| `facet describe` | show a capability's inputs, verification and licences |
| `facet skill` | print the skill for facet or one package |
| `facet doctor` | check facet, the tool chain and this machine's readiness |
| `facet check` | re-verify an asset against its record |
| `facet run` | turn a piece file into verified assets with records |
| `facet preview` | show an asset to a person through its viewer, or to an agent as data |
| `facet license accept` | accept a model's terms before it may be used |
| `facet license list` | list models that need acceptance, with status |
| `facet registry validate` | validate a registry checkout |
| `facet gc` | remove unused environments and caches |
| `facet rm` | remove an installed tool |

Every verb with its flags, lock behaviour and exit codes is documented in
[docs/commands.md](docs/commands.md); the `--json` envelope and the asset
record are in [docs/contract.md](docs/contract.md). The packages live in
[UniverLab/facetmedia-registry](https://github.com/UniverLab/facetmedia-registry),
pinned at one commit; adding a package to it is one directory and a pull
request — see [docs/adding-a-package.md](docs/adding-a-package.md).

`facet` and `facetmedia` are the same command; `python -m facetmedia`
works too. Add `--json` before or after the command for machine-readable
output.

## License

Apache-2.0. See [LICENSE](LICENSE).
