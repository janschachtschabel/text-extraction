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

## Quick start with Docker

Requires Docker with Compose. Compose starts the API and a Valkey for its shared state.

```bash
cp .env.example .env         # set API_KEYS: the sha256 of your key, see the comments in the file
docker compose up --build    # public API on http://localhost:8080
curl -H "Authorization: Bearer $KEY" http://localhost:8080/v1/info
curl -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"url": "https://example.org/"}' http://localhost:8080/v1/extract
```

The internal port 9090 (`/health`, `/ready`, `/metrics`) is not published. For Kubernetes use the
[Helm chart](deploy/helm/text-extraction/README.md).

## Documentation

- [Architecture](docs/architecture.md)
- [Configuration](docs/configuration.md): environment variables, secrets, ports, headers
- [Helm chart](deploy/helm/text-extraction/README.md)
- [CLAUDE.md](CLAUDE.md): rules and workflow for contributors

## Licence

Apache-2.0. See [LICENSE](LICENSE).
