# Contributing to Parallax

Thank you for your interest in contributing to Parallax. This document explains how to set up your environment, run the test suite, and get a change merged.

---

## Dev Setup

### 1. Clone and create a virtual environment

**Windows (PowerShell / cmd):**

```bat
git clone https://github.com/<your-user>/parallax-kernel.git
cd parallax-kernel
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev,server]"
```

**Linux / macOS:**

```bash
git clone https://github.com/<your-user>/parallax-kernel.git
cd parallax-kernel
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev,server]'
```

### 2. Bootstrap a test instance

```bash
python bootstrap.py /tmp/my-parallax
```

---

## Running Tests

The full suite enforces an **80% coverage floor** via `pyproject.toml`. A PR that drops coverage below this threshold will fail CI.

```bash
# Full suite with coverage gate (what CI runs)
pytest

# Detailed missing-line report
pytest --cov=parallax --cov-report=term-missing

# Lint (must be clean before opening a PR)
ruff check .
```

The SQL acceptance harness lives in `tests/acceptance/` and proves the 6-object schema at the DB layer:

```bash
python -m pytest tests/acceptance/ -q
```

GitHub Actions runs the full suite on Python 3.11 for every PR and every push to `main` (`.github/workflows/tests.yml`).

---

## ADR Workflow

Architecture Decision Records live in [`docs/adr/`](docs/adr/). Read [`docs/adr/README.md`](docs/adr/README.md) for the full lifecycle, numbering convention, and six-section template.

**When to write a new ADR:**

- A code review forces a non-obvious tradeoff a future reader will re-debate.
- A schema constraint is frozen into `schema.sql` and removing it would require a migration.
- A test asserts an invariant whose *reason* is not obvious from the test body.
- A public API boundary chooses one shape out of several reasonable ones.

New decisions that freeze a non-obvious tradeoff **require a numbered ADR** before the PR is merged. Style choices covered by the linter, and decisions that are fully self-explanatory from the code, do not.

---

## PR Checklist

Before marking a PR ready for review, verify all of the following:

- [ ] Tests added or updated for every changed behavior.
- [ ] `pytest` passes with coverage staying at or above **80%**.
- [ ] `ruff check .` reports zero errors.
- [ ] `CHANGELOG.md` has an entry under `## [Unreleased]` describing the change.
- [ ] If the PR changes or replaces a frozen contract (schema, public API, migration contract, retrieval invariant), an ADR is updated or a new one is added.

---

## Commit Convention

Parallax uses [Conventional Commits](https://www.conventionalcommits.org/):

```
feat: add by_timeline microsecond boundary fix
fix: escape LIKE wildcards in by_file and by_entity
docs: add ADR-006 retrieval-filtered pipeline
refactor: extract _iso_normalize into shared util
test: pin by_timeline ISO-variant equivalence regression
chore: bump pydantic>=2 in runtime dependencies
```

The type prefix (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`) is required. Keep the subject line under 72 characters and use the body for context when the change is non-obvious.

---

## License

By contributing you agree your code ships under MIT (see [`LICENSE`](LICENSE)).
