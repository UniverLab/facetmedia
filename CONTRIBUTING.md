# Contributing to facetmedia

Thank you for your interest in contributing!

## Development workflow

```bash
git clone https://github.com/UniverLab/facetmedia.git
cd facetmedia
uv sync
uv run pytest
```

Trunk-based: branch off `main` and open the PR against `main`.

## CI/CD and required secrets

This project uses GitHub Actions for CI (`.github/workflows/ci.yml`).

### Required repository secrets

| Secret | Description | Where to get it |
|---|---|---|
| `PYPI_API_TOKEN` | API token to publish packages to [PyPI](https://pypi.org) | [pypi.org/manage/account/token](https://pypi.org/manage/account/token/) → API Tokens → New Token |

Configure secrets at:  
`https://github.com/UniverLab/facetmedia/settings/secrets/actions`

> **Tip:** If you use [ghscaff](https://github.com/UniverLab/ghscaff), you can run `ghscaff apply` to configure missing secrets interactively, or set the env var before running:
> ```bash
> export PYPI_API_TOKEN=<your_token>
> ghscaff apply
> ```

## Release process

Releases are automated via the `publish.yml` workflow. To cut a release:

1. Bump the version in `pyproject.toml`
2. Commit: `chore: release vX.Y.Z`
3. Open PR to `main` — on merge, the workflow publishes to PyPI

## Acceptance

v0.1 is done when its success metric is measured, not assumed: from a clean
Linux with only `uv` installed, one command produces a voice clip that
transcribes with a word error rate under 5 %, in under five minutes, without a
GPU. `scripts/acceptance.sh` reproduces that condition and proves it.

```bash
bash scripts/acceptance.sh             # build the wheel, clean home, remove it
bash scripts/acceptance.sh --keep      # keep the root to inspect assets and logs
bash scripts/acceptance.sh --wheel dist/facetmedia-0.1.0b1-py3-none-any.whl
bash scripts/acceptance.sh --registry-ref main
```

The script builds the wheel, points `HOME`, `FACET_HOME` and every uv cache at
one temporary root, installs the wheel with `uv tool install`, and times
`facet run kokoro.speak tests/acceptance/voz.toml` — including the registry's
first-use download. It then runs the other v0.1 packages' examples (Whisper,
FFmpeg, CAD, ASCII) from the registry snapshot, re-hashing every input and
checking every preview, and finally checks `facet list --available` and
`facet skill kokoro`.

It downloads about 2 GB of models and packages; with every environment unpacked
the temporary root reaches about 3.5 GiB. Run it **alone**, one heavy job at a
time on the lab machine.

Set `GITHUB_TOKEN` in the environment to stay clear of the GitHub API's
anonymous limit (60 requests an hour per IP), which a shared runner reaches
easily. The script passes the token through to the registry download and never
prints it.

`.github/workflows/acceptance.yml` runs the same script on a fresh
`ubuntu-latest` runner and uploads the voice envelope and the run logs. It is
`workflow_dispatch` only: GitHub offers the button once the workflow is on the
default branch, so the first green run happens by hand after the merge.

## Code style

```bash
uv run ruff format .
uv run ruff check .
uv run mypy src/
uv run pytest
```

All four must pass before merging.
