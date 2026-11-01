"""Print the GitHub release notes of a version: its section of ``CHANGELOG.md``, with relative
links made absolute at the tag, then the published images and where to start.

Run from the repository's root: ``python scripts/release_notes.py <X.Y.Z>``. Exit code 1 when the
changelog has no section for the version. The ``release`` workflow writes the output to the
release page of the tag ``vX.Y.Z``. Standard library only, so the runner's Python is enough.
"""

import re
import sys
from pathlib import Path

REPOSITORY = "https://github.com/janschachtschabel/text-extraction"
IMAGE = "ghcr.io/janschachtschabel/text-extraction"
_SECTION = re.compile(r"^## \[")
_LINK = re.compile(r"\]\(([^)\s]+)\)")
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:")
_BLOCK_START = re.compile(r"^(#{1,6} |[-*+] |\d+\. |\||>|```)")


def _unwrap(text: str) -> str:
    """Join hard-wrapped lines: GitHub keeps every line break of a release text. A line
    continues the one before it unless either is blank, it starts a block (heading, list
    item, table row, quote, fence) or the one before is a heading; fenced code stays as is."""
    out: list[str] = []
    fenced = False
    for line in text.splitlines():
        if line.startswith("```"):
            fenced = not fenced
            out.append(line)
            continue
        previous = out[-1] if out else ""
        joins = (
            not fenced
            and line.strip()
            and previous.strip()
            and not previous.startswith("#")
            and not previous.startswith("```")
            and not _BLOCK_START.match(line.lstrip())
        )
        if joins:
            out[-1] = f"{previous.rstrip()} {line.strip()}"
        else:
            out.append(line)
    return "\n".join(out)


def _absolute(text: str, tag: str) -> str:
    """Links relative to the repository's root point at the files of ``tag``; a bare
    ``#anchor`` points into CHANGELOG.md there."""

    def replace(match: re.Match[str]) -> str:
        target = match.group(1)
        if _SCHEME.match(target):
            return match.group(0)
        if target.startswith("#"):
            target = "CHANGELOG.md" + target
        return f"]({REPOSITORY}/blob/{tag}/{target})"

    return _LINK.sub(replace, text)


def _images(version: str) -> str:
    minor = version.rsplit(".", 1)[0]
    guide = f"{REPOSITORY}/blob/main/docs/user-guide"
    chart = f"{REPOSITORY}/blob/main/deploy/helm/text-extraction/README.md"
    compose = "docker compose -f docker-compose.yml -f docker-compose.release.yml up -d --wait"
    return f"""### Images

Five images, each tagged `{version}` and `{minor}` (linux/amd64):

- `{IMAGE}:{version}`: the API and the extract and llm workers
- `{IMAGE}-browser:{version}`: the browser worker (Chromium, and Chrome for unblock mode)
- `{IMAGE}-docling:{version}`: the Docling worker (OCR, no models inside)
- `{IMAGE}-asr:{version}`: the ASR worker (faster-whisper, no models inside)
- `{IMAGE}-nlp:{version}`: the NLP worker (Presidio and spaCy, no models inside)

### Start

- Docker Compose with these images, from a checkout of the tag, or of `main` for a
  tag without `docker-compose.release.yml`: `{compose}`
  ([installation guide]({guide}/en/installation.md), [German]({guide}/de/installation.md)).
- Kubernetes: the Helm chart with `image.tag: "{version}"` ([chart README]({chart})).
"""


def release_notes(changelog: str, version: str) -> str:
    """The notes of ``version``. Raises LookupError when the changelog has no section
    ``## [version] - <date>``."""
    heading = re.compile(rf"^## \[{re.escape(version)}\] - \S")
    lines = changelog.splitlines()
    start = next((i for i, line in enumerate(lines) if heading.match(line)), None)
    if start is None:
        raise LookupError(f"CHANGELOG.md has no section for {version}")
    end = next(
        (i for i in range(start + 1, len(lines)) if _SECTION.match(lines[i])),
        len(lines),
    )
    section = "\n".join(lines[start + 1 : end]).strip("\n")
    return _unwrap(f"{_absolute(section, f'v{version}')}\n\n{_images(version)}") + "\n"


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: release_notes.py <X.Y.Z>", file=sys.stderr)
        return 2
    changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
    try:
        notes = release_notes(changelog, argv[0])
    except LookupError as error:
        print(error, file=sys.stderr)
        return 1
    sys.stdout.write(notes)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
