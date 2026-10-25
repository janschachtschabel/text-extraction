"""The models of the Docling worker on their volume (ruling F15): download, verify, marker.

``models.toml`` (next to this module) lists every file with a URL at a fixed commit, its byte
size and its SHA-256. ``download_models`` fills ``DOCLING_MODELS_PATH`` from it, in the init
container that mounts the volume writable (``text-extraction-docling-worker
--download-models``):

1. With the marker ``.complete-<manifest hash>`` present it does nothing: the volume holds this
   manifest's files already. Another manifest (a new pin) has another marker.
2. Otherwise every file is downloaded into a staging directory under the volume
   (``.download-*``), checked against its size and SHA-256 while it is written. One file that
   does not match fails the run: nothing is moved, no marker is written, the staging directory
   is removed.
3. The old markers are removed, then each top-level directory (``docling/``, ``tessdata/``) is
   renamed into place, replacing the old one; then the marker is written. A run cut in between
   leaves no marker, so the next run downloads again.

Several init containers may share one ReadWriteMany volume (replicas started at once). One that
finds the marker after its downloads keeps the other's files and discards its staging; a
failure while it moves its files into place (``OSError``) is ``ModelDownloadFailed``, unless the
marker appeared meanwhile: then the other container won, and the volume is complete.

The downloads use plain httpx, not the SSRF-guarded fetcher of the pipeline: the URLs are ours
(the manifest), never a caller's, and an operator may route them through a proxy (``HTTPS_PROXY``,
which httpx reads). They are https only (a manifest with an http URL is refused; tests opt in
with ``allow_http``), and so is every redirect they follow (Hugging Face serves its files from a
CDN); the size and the hash decide what is kept. Each file has a total deadline that grows with
its size (``file_deadline_seconds``: 120 s plus 256 KiB/s), on top of the 60 s for each read, so
a mirror that trickles cannot hold the init container.

``missing_models`` is the worker's check before ``/ready``: the files of the manifest that are
absent or have another size (the hash of the files is not read at every start).
``require_models`` repeats that check in the Docling child before it builds a converter, so a
file that went missing later is ``ModelsMissing`` (Docling itself raises a plain
``RuntimeError`` for a missing ``config.json``, which would read as an unreadable document).

Logs name the files' paths in the manifest and the volume's path, never a URL.
"""

import hashlib
import os
import re
import shutil
import tempfile
import time
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib import resources
from pathlib import Path, PurePosixPath
from typing import Any, Final

import httpx
import structlog

_log = structlog.get_logger(__name__)

_MARKER_PREFIX: Final = ".complete-"
_STAGING_PREFIX: Final = ".download-"
_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")
# A slow mirror may take long for a large file, but not for its first byte.
_TIMEOUT: Final = httpx.Timeout(60.0, connect=15.0)
# The total time of one file: a fixed part, then the slowest rate accepted.
_FILE_BASE_SECONDS: Final = 120.0
_MIN_BYTES_PER_SECOND: Final = 256 * 1024


class ModelsMissing(Exception):  # noqa: N818 - a state of the volume, named as such
    """A model file of the manifest is not on the models volume. The Docling worker maps this
    type name to ``internal_error`` (retryable)."""


class ModelDownloadFailed(Exception):  # noqa: N818 - a failed run, named as such
    """A model file could not be downloaded, or did not match its size or SHA-256."""


@dataclass(frozen=True)
class ModelFile:
    path: str  # relative to the volume, with "/" separators
    url: str
    sha256: str
    size: int


@dataclass(frozen=True)
class Manifest:
    files: tuple[ModelFile, ...]
    digest: str  # SHA-256 of the manifest's bytes

    @property
    def marker(self) -> str:
        """The name of the file that says the volume holds this manifest's files."""
        return f"{_MARKER_PREFIX}{self.digest[:16]}"


def load_manifest(data: bytes | None = None, *, allow_http: bool = False) -> Manifest:
    """The manifest from ``data`` (TOML), by default the built-in ``models.toml``. Raises
    ``ValueError`` for an entry that is not a safe relative path, an https URL (http too with
    ``allow_http``, for tests against a local server), a SHA-256 in lower-case hex and a positive
    size."""
    if data is None:
        data = resources.files("text_extraction.documents").joinpath("models.toml").read_bytes()
    parsed = tomllib.loads(data.decode("utf-8"))
    schemes = ("https", "http") if allow_http else ("https",)
    files = tuple(_model_file(entry, schemes) for entry in parsed.get("files", []))
    if len({file.path for file in files}) != len(files):
        raise ValueError("the models manifest names a path twice")
    return Manifest(files=files, digest=hashlib.sha256(data).hexdigest())


def _model_file(entry: Mapping[str, Any], schemes: tuple[str, ...]) -> ModelFile:
    path, url, sha256, size = (
        entry.get("path"),
        entry.get("url"),
        entry.get("sha256"),
        entry.get("size"),
    )
    if not isinstance(path, str) or not _safe_path(path):
        raise ValueError("a models manifest path must be relative, without '..'")
    if not isinstance(url, str) or httpx.URL(url).scheme not in schemes:
        raise ValueError(f"the models manifest entry {path} needs an https URL")
    if not isinstance(sha256, str) or not _SHA256.match(sha256):
        raise ValueError(f"the models manifest entry {path} needs a SHA-256 in hex")
    if type(size) is not int or size < 1:
        raise ValueError(f"the models manifest entry {path} needs a positive size")
    return ModelFile(path=path, url=url, sha256=sha256, size=size)


def _safe_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return (
        bool(parts)
        and not path.startswith("/")
        and "\\" not in path
        and all(part not in ("", ".", "..") for part in parts)
        and len(parts) >= 2  # under a top-level directory, which is replaced as a whole
    )


def missing_models(root: Path, manifest: Manifest) -> list[str]:
    """The manifest's paths that are not under ``root`` with their size, sorted."""
    missing = []
    for file in manifest.files:
        target = root / file.path
        if not target.is_file() or target.stat().st_size != file.size:
            missing.append(file.path)
    return sorted(missing)


def require_models(root: Path, manifest: Manifest | None = None) -> None:
    """Raise ``ModelsMissing`` unless every file of the manifest (by default the built-in one)
    is under ``root`` with its size. The message counts the files; it names no path."""
    manifest = manifest or load_manifest()
    missing = missing_models(root, manifest)
    if missing:
        raise ModelsMissing(
            f"{len(missing)} of {len(manifest.files)} model files are missing on the models volume."
        )


def file_deadline_seconds(size: int) -> float:
    """The total time one file of ``size`` bytes may take to download."""
    return _FILE_BASE_SECONDS + size / _MIN_BYTES_PER_SECOND


def download_models(
    root: Path,
    manifest: Manifest | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """Put the manifest's files under ``root`` (see the module docstring). True when it
    downloaded them, False when the marker said they are there. Raises
    ``ModelDownloadFailed``; ``root`` then has no marker and no new file. ``clock`` measures
    each file's deadline."""
    manifest = manifest if manifest is not None else load_manifest()
    root.mkdir(parents=True, exist_ok=True)
    marker = root / manifest.marker
    if marker.is_file():
        _log.info("docling_models_present", models_path=str(root))
        return False
    staging = Path(tempfile.mkdtemp(prefix=_STAGING_PREFIX, dir=root))
    try:
        with httpx.Client(follow_redirects=True, timeout=_TIMEOUT, transport=transport) as client:
            for file in manifest.files:
                _download(client, file, staging / file.path, clock)
        if marker.is_file():  # another init container on the volume finished meanwhile
            _log.info("docling_models_present", models_path=str(root))
            return False
        try:
            _swap(root, staging, manifest)
        except OSError as error:
            if marker.is_file():  # the other container's swap won the race
                _log.info("docling_models_present", models_path=str(root))
                return False
            raise ModelDownloadFailed(
                f"moving the files into place: {type(error).__name__}"
            ) from None
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    _log.info("docling_models_downloaded", models_path=str(root), files=len(manifest.files))
    return True


def _swap(root: Path, staging: Path, manifest: Manifest) -> None:
    """Remove the old markers, rename each top-level directory from ``staging`` into place,
    then write the marker."""
    for old in root.glob(f"{_MARKER_PREFIX}*"):
        old.unlink()
    for top in sorted({PurePosixPath(file.path).parts[0] for file in manifest.files}):
        _replace(staging / top, root / top, staging)
    written = staging / manifest.marker
    written.write_text(f"{manifest.digest}\n", encoding="utf-8")
    os.replace(written, root / manifest.marker)


def _download(
    client: httpx.Client, file: ModelFile, target: Path, clock: Callable[[], float]
) -> None:
    """Stream one file to ``target``, checking its size, SHA-256 and deadline on the way."""
    target.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    seconds = file_deadline_seconds(file.size)
    deadline = clock() + seconds
    try:
        with client.stream("GET", file.url) as response, target.open("wb") as out:
            if httpx.URL(file.url).scheme == "https" and response.url.scheme != "https":
                raise ModelDownloadFailed(f"{file.path}: redirected away from https")
            if response.status_code != 200:
                raise ModelDownloadFailed(f"{file.path}: HTTP status {response.status_code}")
            for chunk in response.iter_bytes():  # as it arrives, so the deadline is seen
                if clock() > deadline:
                    raise ModelDownloadFailed(
                        f"{file.path}: did not arrive within {round(seconds)} seconds"
                    )
                size += len(chunk)
                if size > file.size:
                    raise ModelDownloadFailed(f"{file.path}: more than {file.size} bytes")
                digest.update(chunk)
                out.write(chunk)
    except httpx.HTTPError as error:
        raise ModelDownloadFailed(f"{file.path}: {type(error).__name__}") from None
    if size != file.size:
        raise ModelDownloadFailed(f"{file.path}: {size} bytes instead of {file.size}")
    if digest.hexdigest() != file.sha256:
        raise ModelDownloadFailed(f"{file.path}: the SHA-256 does not match the manifest")
    _log.info("docling_model_downloaded", path=file.path, size=size)


def _replace(source: Path, target: Path, staging: Path) -> None:
    """Rename ``source`` to ``target``; an old ``target`` is moved into ``staging`` first (and
    removed with it). Both are on the volume, so each rename is atomic."""
    if target.exists():
        os.replace(target, staging / f".old-{target.name}")
    os.replace(source, target)
