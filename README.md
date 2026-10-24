# facetmedia

facet is the lab's "ollama" for creation tools. It installs each tool (CadQuery, Kokoro, ...) in its own pinned uv environment and exposes them to agents through one verifiable `--json` contract.

> Internal preview: the interface may change between betas.

## Install

```bash
uv tool install facetmedia
```

Linux and WSL only. Everything facet installs or remembers lives under one directory, `~/.facet` (override with `FACET_HOME`); see [docs/home.md](docs/home.md) for the layout, the per-invocation logs, the lock, and where facet refuses to run. The packages live in [UniverLab/facetmedia-registry](https://github.com/UniverLab/facetmedia-registry), which `facet update` pins at one commit; see [docs/registry.md](docs/registry.md) for the registry layout, manifest format, and `facet registry validate`. `facet doctor` reports whether this machine can run a capability — facet's own health, the readiness checks and each installed package's `doctor` verb, with a fix for every failure; see [docs/doctor.md](docs/doctor.md) for the checks, the depths and the `--json` data. `facet check` re-verifies an asset facet did not make against its record; see [docs/check.md](docs/check.md). `facet list` and `facet describe` are the discovery surface: what the packages can do and one capability's full contract; see [docs/discover.md](docs/discover.md). `facet pull` installs one package into its own pinned environment; see [docs/pull.md](docs/pull.md) for what it checks, builds, and downloads. `facet run` turns a declarative piece file into verified assets with their records; see [docs/run.md](docs/run.md) for the flags, the order of steps, the exit codes, and the runner protocol. `facet preview` shows one asset to an agent as data or to a person through the capability's viewer; see [docs/preview.md](docs/preview.md) for the two forms and the paths the viewer server serves. Models with terms are accepted with `facet license accept`; see [docs/license.md](docs/license.md). Environments are persistent, and `facet rm` and `facet gc` are how the disk comes back; see [docs/rm.md](docs/rm.md) and [docs/gc.md](docs/gc.md).

## Commands

| Command | Purpose |
|---------|---------|
| `facet update` | download the registry at a pinned commit |
| `facet pull` | install a tool into its own pinned environment |
| `facet list` | list installed and available tools |
| `facet describe` | show a capability's inputs, verification and licences |
| `facet doctor` | report facet, this machine's readiness and each package's doctor |
| `facet check` | re-verify an asset against its record |
| `facet run` | turn a piece file into verified assets with records |
| `facet preview` | show an asset to a person through its viewer, or to an agent as data |
| `facet license accept` | accept a model's terms before it may be used |
| `facet license list` | list models that need acceptance, with status |
| `facet registry validate` | validate a registry checkout |
| `facet gc` | remove unused environments and caches |
| `facet rm` | remove an installed tool |

`facet` and `facetmedia` are the same command; `python -m facetmedia` works too. Add `--json` before or after the command for machine-readable output (see [docs/contract.md](docs/contract.md)).

## License

Apache-2.0. See [LICENSE](LICENSE).
