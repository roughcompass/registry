# Contributing to capability-fabric

## Developer Certificate of Origin (DCO)

Every commit must carry a `Signed-off-by:` trailer certifying the [Developer Certificate of Origin v1.1](https://developercertificate.org/). Sign your commit with:

```
git commit -s -m "your message"
```

The trailer must use the same name and email as your git author identity. The DCO check runs as a GitHub Action on every pull request; commits without a valid `Signed-off-by:` block the PR.

There is no Contributor License Agreement (CLA) — DCO is the only contributor sign-off mechanism.

## Development setup

```bash
cd capability-fabric
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Code style

- Python 3.12+, formatted with `ruff format`, linted with `ruff check`.
- `mypy --strict` over `fabric/` and `sync/`.
- Tests: `pytest tests/unit` for fast feedback; `pytest tests/integration` requires Docker (testcontainers spins up Postgres + pgvector).

## Commit messages

Tasks ship one-commit-per-task. The message starts with the task ID, e.g.:

```
CAP-PN-TNN: short description of what changed

<body explaining why>

Signed-off-by: Your Name <you@example.com>
```
