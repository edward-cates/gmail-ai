# CLAUDE.md

## Dev Environment

Use the `gmail-ai` conda env for all commands:

```bash
source ~/miniconda3/etc/profile.d/conda.sh && conda activate gmail-ai
```

## Validation

Always run validation through make:

```bash
make validate   # tests + lint (the full suite)
make test       # tests only
make lint       # lint only
```

Never run pytest or ruff directly — `make` uses `uv run` and ensures correct paths/deps.
