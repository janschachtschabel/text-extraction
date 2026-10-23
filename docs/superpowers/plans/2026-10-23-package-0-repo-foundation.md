# Package 0: Repo Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Python 3.13 uv project with lint, format, type and test gates that pass locally and in GitHub Actions, so every later package lands on a green, enforced baseline.

**Architecture:** `src/` layout with one package `text_extraction`; quality tools configured in `pyproject.toml`; one CI workflow that runs exactly the commands a contributor runs locally. No runtime code beyond the version string.

**Tech Stack:** uv 0.11, Python 3.13, ruff, mypy (strict), pytest, pre-commit, GitHub Actions.

**Spec:** `docs/architecture.md`, `docs/packages.md` (row 0), `CLAUDE.md`

## Global Constraints

- English only in code, comments, docs and commit messages.
- Python `>=3.13,<3.14`; `.python-version` = `3.13`.
- Package name `text-extraction`, import name `text_extraction`, version `0.1.0`.
- No AGPL/GPL dependencies. Project licence: Apache-2.0 (root `LICENSE`).
- Images: tagged SemVer for production, `latest` only for staging (applies from package 1, where the first Dockerfile lands).

## Review Focus

1. A stale `uv.lock` → CI must fail (`uv sync --locked`), not silently re-resolve.
2. Jan develops on Windows → `.gitattributes` forces LF for source files so ruff format does not flip line endings.
3. mypy strict must cover `tests/` too, otherwise later tests drift untyped.
4. pre-commit hooks must run the same tool versions as CI (call `uv run`, not separate hook repos with their own pins).
5. `__version__` must come from installed metadata, so pyproject stays the single version source.

Deviation from `docs/packages.md`: the image build workflow moves to package 1, because package 0 has no Dockerfile to build. Task 4 updates the table.

---

### Task 1: Project scaffold and version

**Files:**
- Create: `pyproject.toml`, `.python-version`, `.gitignore`, `.gitattributes`, `src/text_extraction/__init__.py`, `src/text_extraction/py.typed`, `tests/__init__.py`, `tests/test_version.py`, `uv.lock` (generated)

**Interfaces:**
- Produces: `text_extraction.__version__: str` (from `importlib.metadata.version("text-extraction")`)

- [ ] **Step 1: Write the failing test** in `tests/test_version.py`

```python
from importlib.metadata import version

import text_extraction


def test_version_matches_package_metadata() -> None:
    assert text_extraction.__version__ == version("text-extraction")


def test_version_is_semver() -> None:
    parts = text_extraction.__version__.split(".")
    assert len(parts) == 3
    assert all(part.isdigit() for part in parts)
```

- [ ] **Step 2: Create `pyproject.toml`** with `[project]` (name, version `0.1.0`, description, `requires-python = ">=3.13,<3.14"`, `license = "Apache-2.0"` (root LICENSE), no runtime deps), `[build-system]` `uv_build`, `[tool.uv.build-backend] module-root = "src"`, `[dependency-groups] dev = ["pytest", "ruff", "mypy", "pre-commit"]`.
- [ ] **Step 3: Run `uv sync` then `uv run pytest -q`** — Expected: FAIL (`AttributeError: __version__`).
- [ ] **Step 4: Implement `__version__`** in `src/text_extraction/__init__.py`; add empty `py.typed`.
- [ ] **Step 5: Run `uv run pytest -q`** — Expected: 2 passed.
- [ ] **Step 6:** `.python-version` (`3.13`), `.gitignore` (venv, caches, `dist/`, `.env`), `.gitattributes` (`* text=auto eol=lf`, `*.cmd eol=crlf`).
- [ ] **Step 7: Commit** `feat: add uv project scaffold with version`.

### Task 2: Quality gates

**Files:**
- Modify: `pyproject.toml` (tool sections)
- Create: `.pre-commit-config.yaml`

- [ ] **Step 1:** `[tool.ruff]` `line-length = 100`, `target-version = "py313"`, `src = ["src", "tests"]`; `[tool.ruff.lint] select = ["E", "W", "F", "I", "B", "UP", "S", "SIM", "RUF", "N", "ASYNC", "PT"]`, per-file ignore `S101` in `tests/**`.
- [ ] **Step 2:** `[tool.mypy] strict = true`, `python_version = "3.13"`, `files = ["src", "tests"]`.
- [ ] **Step 3:** `[tool.pytest.ini_options] testpaths = ["tests"]`, `addopts = "-ra --strict-markers --strict-config"`, `xfail_strict = true`.
- [ ] **Step 4:** `.pre-commit-config.yaml` with local hooks (`language: system`): `uv run ruff check --fix`, `uv run ruff format`, `uv run mypy`; plus `pre-commit-hooks` `check-yaml`, `end-of-file-fixer`, `trailing-whitespace`, `check-added-large-files` (`--maxkb=500`).
- [ ] **Step 5: Verify** `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q` — Expected: all clean, 2 passed. Then add a deliberate unused import to a scratch copy and confirm `ruff check` fails (proves the gate bites); remove it.
- [ ] **Step 6: Commit** `chore: configure ruff, mypy strict, pytest and pre-commit`.

### Task 3: CI workflow

**Files:**
- Create: `.github/workflows/ci.yml`

- [ ] **Step 1:** Workflow on `push` to `main` and `pull_request`; `permissions: contents: read`; `concurrency` per ref with cancel-in-progress; one job on `ubuntu-latest`: checkout, `astral-sh/setup-uv` (pinned major, cache on), `uv sync --locked`, then `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, `uv run pytest -q`.
- [ ] **Step 2: Verify locally** `uv sync --locked` passes on the committed lock; then touch `pyproject.toml` deps without relocking in a scratch branch and confirm `uv sync --locked` fails (Review Focus 1); restore.
- [ ] **Step 3: Commit** `ci: run lint, format, type and test gates`.
- [ ] **Step 4: Verify on GitHub** after the PR is opened: the `ci` check is green.

### Task 4: Developer docs

**Files:**
- Modify: `CLAUDE.md` (Commands section), `docs/packages.md` (rows 0 and 1)
- Create: `README.md`, `.dockerignore`

- [ ] **Step 1:** `CLAUDE.md` Commands: `uv sync`, `uv run pytest`, `uv run ruff check .`, `uv run ruff format .`, `uv run mypy`, `uv run pre-commit install`.
- [ ] **Step 2:** `docs/packages.md`: move "image build with SemVer tags; `latest` only for staging" from row 0 to row 1.
- [ ] **Step 3:** `README.md`: one-paragraph purpose, status ("under construction, see docs/packages.md"), quick start with the commands above, links to `docs/architecture.md` and `CLAUDE.md`.
- [ ] **Step 4:** `.dockerignore`: `.git`, `.venv`, caches, `tests/`, `docs/`, `.claude/`, `*.md` except `README.md`.
- [ ] **Step 5: Verify** all four gates still pass.
- [ ] **Step 6: Commit** `docs: add README and developer commands`.
