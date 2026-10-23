# text-extraction

A FastAPI service that turns web pages, files, audio and video into clean text or
Markdown, with a fast HTTP mode and a browser mode.

## Status

Under construction. See [docs/packages.md](docs/packages.md) for the implementation
packages and their progress.

## Quick start

Requires Python 3.13 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                      # install dependencies into .venv
uv run pytest                # run the tests
uv run ruff check .          # lint
uv run ruff format .         # format
uv run mypy                  # type-check (strict)
uv run pre-commit install    # install the git hooks
```

## Documentation

- [Architecture](docs/architecture.md)
- [CLAUDE.md](CLAUDE.md): rules and workflow for contributors

## Licence

Apache-2.0. See [LICENSE](LICENSE).
