# facetmedia

facet is the lab's "ollama" for creation tools. It installs each tool (CadQuery, Kokoro, ...) in its own pinned uv environment and exposes them to agents through one verifiable `--json` contract.

> Internal preview: the interface may change between betas.

## Install

```bash
uv tool install facetmedia
```

Linux and WSL only. Everything facet installs or remembers lives under one directory, `~/.facet` (override with `FACET_HOME`); see [docs/home.md](docs/home.md) for the layout, the per-invocation logs, the lock, and where facet refuses to run. The packages live in [UniverLab/facetmedia-registry](https://github.com/UniverLab/facetmedia-registry), which `facet update` pins at one commit; see [docs/registry.md](docs/registry.md) for the registry layout, manifest format, and `facet registry validate`. `facet pull` installs one package into its own pinned environment; see [docs/pull.md](docs/pull.md) for what it checks, builds, and downloads. Models with terms are accepted with `facet license accept`; see [docs/license.md](docs/license.md).

## Commands

| Command | Purpose |
|---------|---------|
| `facet update` | download the registry at a pinned commit |
| `facet pull` | install a tool into its own pinned environment |
| `facet list` | list installed and available tools |
| `facet describe` | show a tool's manifest |
| `facet check` | verify an installed tool |
| `facet run` | run a tool through the --json contract |
| `facet preview` | render a tool's output for a person |
| `facet license accept` | accept the license of a tool |
| `facet license list` | list the licenses of installed tools |
| `facet registry validate` | validate a registry checkout |
| `facet gc` | remove unused environments and caches |
| `facet rm` | remove an installed tool |

`facet` and `facetmedia` are the same command; `python -m facetmedia` works too. Add `--json` before or after the command for machine-readable output (see [docs/contract.md](docs/contract.md)).

## License

Apache-2.0. See [LICENSE](LICENSE).
