# facetmedia

> 

## Getting started

### Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

### Install

```bash
uv sync
```

### Run CLI

```bash
uv run facetmedia --name world
```

### Test

```bash
uv run pytest
```

## Development

### Format & lint

```bash
uv run ruff format .
uv run ruff check .
```

### Type check

```bash
uv run mypy src/
```

### Quality gate

CI runs the lab's strict gate ([UniverLab/workflows](https://github.com/UniverLab/workflows/blob/main/docs/quality-gate.md)): an 80 % coverage gate, file and function size, complexity, unused dependencies, known vulnerabilities and licences. Commit `uv.lock` (`uv lock`): CI installs with `--frozen`. To run the gate locally:

```bash
uv run pytest --cov
bash path/to/UniverLab/workflows/scripts/python-quality.sh
```

## Project structure

```
facetmedia/
├── pyproject.toml
├── src/
│   └── facetmedia/
│       ├── __init__.py
│       ├── core.py       # Core logic
│       └── cli.py        # CLI entry point
├── tests/
│   ├── test_cli.py
│   └── test_core.py
└── README.md
```

## License

This project is licensed under the MIT License — see LICENSE for details.
