# Project conventions — `registry/`

This is the shipping product repo. **Remote: `roughcompass/registry`.** Everything in this directory is part of the application; the test pyramid, gates, and runbooks all live here.

Planning artifacts (PRDs, TDDs, ADRs, dev plans) live in a **separate** repo at `../.context/`. That repo has no remote by default. Code in here must never reference paths or labels from there — see the doc-refs rule below.

---

## No external-doc references in shipped code

**The rule.** Files in this repo must not contain references to documents that live in `../.context/` — those are internal planning docs not visible to a future engineer reading this repo in isolation.

**What "shipped" means in this repo:**

| Path | Shipped? | Rule applies? |
|---|---|---|
| `registry/**/*.py` | yes | yes |
| `**/*.md` (operator-facing docs, runbooks) | yes | yes |
| `eval/EVAL.md` | yes | yes — *except* `CAP-PN-TNN` task IDs are allowed in a "Commits" column as git-history anchors |
| `.env.example` | yes | yes |
| `README.md` | yes | yes |
| Past git commit messages | yes, but immutable | no — historical record; rule applies to new commits going forward |

**Forbidden patterns.** Any of these in a tracked file is a violation:

```
\bADR-\d+\b                           # e.g. ADR-024
\bF\d+\.\d+\b                         # e.g. F7.12 (PRD feature numbers)
\bOQ-[A-Za-z0-9-]+                    # e.g. OQ-P7-3 (open-question labels)
\bCAP-P\d+R?-T\d+[a-z]?\b             # e.g. CAP-P7-T20 (dev-plan task IDs, except EVAL.md)
\bCC-T\d+\b                           # e.g. CC-T02
\bDRC-T\d+\b                          # e.g. DRC-T03
\bAQ\d+\b                             # e.g. AQ7 (architecture-quality labels)
PRD §                                 # explicit doc citation
TDD §                                 # explicit doc citation
(interfaces|flows|data-model)\.md §   # explicit doc/section citation
\bPhase \d+\b                         # bare phase labels — say what the change is, not which phase
```

**The principle.** Comments should explain *the rule*, in the code's own vocabulary. Not where the rule was decided.

**Why this rule exists.** Planning docs live in a different repo. A future engineer (or agent) reading the code in isolation cannot resolve `ADR-024` or `F7.12` — those references imply context that has gone missing. The code must carry the rationale itself.

### Before / after

**Before (banned):**

```python
# Visibility filter is the ADR-024 chokepoint — every cross-tenant
# query path must call filter_entities() per F7.1 / TDD §2.1.
```

**After (acceptable):**

```python
# Every cross-tenant query path funnels through filter_entities() so
# tenant-isolation enforcement lives at one layer. Bypassing this
# function is how data leaks between tenants happen.
```

### Intentional bypass

If citing an external resource is genuinely the right thing (a stable public URL, an RFC, a Postgres docs page), end the line with the marker:

```python
# Postgres ROW SECURITY isn't used; we enforce at the service layer.  # doc-ref: intentional
```

The validation gate ignores lines tagged this way. Use sparingly.

### Validation

The gate is `scripts/check_no_doc_refs.py`, exposed as `make doc-refs`. It walks the in-scope paths, applies the forbidden-pattern regex set, ignores `# doc-ref: intentional` lines, and exits non-zero with a `file:line` list on any hit.

```
python scripts/check_no_doc_refs.py            # full repo
python scripts/check_no_doc_refs.py --explain  # one line per pattern + fix guidance
python scripts/check_no_doc_refs.py --paths registry/service
```

### Task IDs are commit-history anchors, not doc refs

`CAP-PN-TNN` / `CC-TNN` / `DRC-TNN` task IDs appear in git commit message subjects (`git log --grep=CAP-P7-T20`). Because git history ships with the repo, these IDs remain resolvable forever.

- In code comments: **do not include them**. Anyone can `git blame` to find the commit.
- In `eval/EVAL.md` only: allowed as commit anchors in a dedicated "Commits" column.

---

## Repo navigation — mental model

| Path | What lives there |
|---|---|
| `registry/api/routers/` | HTTP surface — one router per concern. Thin adapters over services. |
| `registry/api/middleware/` | Tenant resolution, rate-limit, HTTP-methods router factory. |
| `registry/service/` | Business logic. **Every cross-tenant query MUST funnel through `service/visibility.py`** — bypassing it is how leaks between tenants happen. |
| `registry/workers/` | Background jobs (webhook delivery, closure-cache refresh). |
| `registry/storage/` | SQLAlchemy models + Alembic migrations under `migrations/versions/`. |
| `registry/security/` | PII scanner (built-in pattern modules + per-tenant policy resolver). |
| `sync/` | External-source ingest connectors. Credentials resolve dynamically from env (`sync/connector.py::resolve_credential`); they don't live in `Settings`. |
| `scripts/` | Operational CLIs. Each script reads config via `get_settings()`; there is no separate config path for scripts. |
| `tests/{unit,integration,conformance,perf}/` | Test pyramid (see below). |
| `.env.example` | Canonical env-var inventory. The example helm chart in `packaging/helm/` mirrors it; other deployment targets do the same. |

The single most important architectural rule: **`service/visibility.py` is the one chokepoint for cross-tenant queries**. If you're writing a new service that returns entity rows, you must funnel through `filter_entities()` or `assert_visible()`.

---

## Testing

Four test buckets with very different purposes — pick the right one when adding a test:

- `tests/unit/` — pure Python, no DB, fast (~1.5s for 1100+ tests). Use SQL-string-keyed `AsyncMock` routers to fake the DB layer. **Default home for new tests.**
- `tests/integration/` — testcontainers Postgres + live FastAPI app via `httpx.ASGITransport`. Use when behaviour spans more than one service or you need real SQL (triggers, constraints, partitioning).
- `tests/conformance/` — contract drift gates (openapi.json snapshot, MCP tool catalog, cross-tenant isolation invariants). Run before tagging a release.
- `tests/perf/` — SLO verification (latency p95s, webhook fan-out). Marked `@pytest.mark.perf @pytest.mark.slow`; excluded from `make test` and reserved for the release pipeline.

When in doubt: write a unit test first. Promote to integration only when the unit version can't exercise the real code path.

**The gates are Make targets.** `make lint`, `make typecheck`, `make doc-refs`, `make test-hygiene`, `make test-unit`, `make test-conformance` are the contract. CI platforms (GitHub Actions, GitLab CI, Jenkins, Buildkite, …) wire these targets — they don't redefine the commands. See `docs/07-contributing/02-ci.md`.

**Test naming rule.** Test files must describe present-tense system behavior, not delivery history. Phase-named test files and stale phase-marker comments are forbidden in `tests/`. The gate is `make test-hygiene` (`scripts/check_no_phase_named_tests.py`). It runs on every commit alongside `make doc-refs`. If a test legitimately uses "phase" as a domain term unrelated to delivery milestones, end the relevant comment line with `# test-hygiene: intentional`.

---

## Secrets and config

- Every env var the app reads lives in `Settings` (`registry/config.py`) and is documented in `.env.example`.
- **Never commit secrets.** Webhook secrets, OIDC discovery URLs, and database passwords are operator-provided at deploy time (Kubernetes Secret, ECS task-definition secret refs, systemd EnvironmentFile, etc.).
- Two documented bypasses to the "everything goes through `Settings`" rule, both tagged `# config: intentional` inline so the consolidation gate ignores them:
  1. `sync/webhook.py` reads `{GITHUB,GITLAB}_WEBHOOK_SECRET` directly to support per-instance secret rotation without an app restart.
  2. `sync/connector.py::resolve_credential` resolves connector credentials by dynamic ref string — the set is not fixed, so it cannot live in `Settings`.
- Any new env-var read outside `Settings` triggers the consolidation gate and must justify the bypass with a same-line `# config: intentional` marker.

---

## Agent vs direct edit

When working with this repo via Claude (or any AI agent), the heuristic for whether to spawn a sub-agent or edit inline:

| Situation | Approach |
|---|---|
| Targeted change in a known file (≤ 5 edits) | Direct `Edit`/`Write` |
| Broad codebase exploration / "where is X?" | `Explore` agent (read-only, fast) |
| Bulk sweep across many files / independent scopes | Parallel `backend` agents, one per non-overlapping scope |
| Planning + design | `architect` / `designer` / `product` (writes to `../.context/` only — different repo) |
| Reviewing existing artefacts | `reviewer` (read + score) |

When delegating, the agent will not see this conversation. Brief it like a colleague who just walked in: include the file paths, what you've already tried, and what you expect back.

---

## Commit boundary

This repo and `../.context/` are **independent**. Never `git add` paths outside this directory — `../.context/...`, `../CLAUDE.md`, `../README.md`, and everything else outside `registry/` belongs to other repos (or nowhere). The planning workspace commits to its own `.git`; nothing else upstream of this directory is tracked.

A typical dev-loop iteration produces two commits:
1. Code change here → commit in this repo, push to `roughcompass/registry`.
2. Task status flip / new defect report → commit in `../.context/` against its own `.git`.

Either commit alone is incomplete; neither commit is shared between repos.

---

## Commit messages

- Subject line prefix with the task ID when applicable: `PP-T03: expand CLAUDE.md`, `CAP-P7-T20: ...`.
- Body explains **why**, not what. The diff already shows what.
- Reference resolved test counts or perf numbers at the end of the body when relevant ("1133 unit tests pass; gate exits 0").
- Task IDs in commit subjects are intentional — `git log --grep=PP-T03` is how `EVAL.md`'s commit-anchor column resolves.

---

## Other conventions

(add new project-wide conventions here as they emerge.)
