"""The models store (``model_store.py``): the manifests of the Docling and the ASR workers, the
check of a volume, and the downloads against a local HTTP server or a mock transport. The tests of
package 5 run unchanged in behaviour against the moved, manifest-generic module."""

import hashlib
import re
from importlib import resources
from pathlib import Path
from typing import get_args

import httpx
import pytest
from pytest_httpserver import HTTPServer

from text_extraction import model_store
from text_extraction.settings import Settings


def manifest_for(server: HTTPServer, files: dict[str, bytes], *, wrong: str = "") -> bytes:
    """A manifest of ``files`` (path: content) served by ``server``; ``wrong`` names a path whose
    hash is given wrong."""
    entries = []
    for path, content in files.items():
        digest = hashlib.sha256(content + (b"x" if path == wrong else b"")).hexdigest()
        server.expect_request(f"/{path}").respond_with_data(content)
        entries.append(
            f'[[files]]\npath = "{path}"\nurl = "{server.url_for("/" + path)}"\n'
            f'sha256 = "{digest}"\nsize = {len(content)}\n'
        )
    return "\n".join(entries).encode()


MODEL_FILES = {
    "tessdata/deu.traineddata": b"deu model" * 100,
    "docling/layout/model.safetensors": b"layout weights" * 100,
}


def filled_volume(root: Path, manifest: model_store.Manifest) -> None:
    """Sparse files of the manifest's sizes: what ``missing_models`` looks at."""
    for file in manifest.files:
        target = root / file.path
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as handle:
            handle.truncate(file.size)


def test_require_models_passes_on_a_complete_volume(tmp_path: Path) -> None:
    manifest = model_store.load_manifest("docling")
    filled_volume(tmp_path, manifest)

    model_store.require_models(tmp_path, manifest)  # no error


def test_require_models_names_a_missing_layout_config(tmp_path: Path) -> None:
    # Docling itself raises a RuntimeError for a missing config.json, which would read as an
    # unreadable document; the check before the converter makes it ModelsMissing.
    manifest = model_store.load_manifest("docling")
    filled_volume(tmp_path, manifest)
    config = "docling/docling-project--docling-layout-heron/config.json"
    (tmp_path / config).unlink()

    with pytest.raises(model_store.ModelsMissing, match="1 of 9") as caught:
        model_store.require_models(tmp_path, manifest)

    assert type(caught.value).__name__ == "ModelsMissing"  # the type name the worker maps
    assert str(tmp_path) not in str(caught.value)


def test_require_models_on_an_empty_volume(tmp_path: Path) -> None:
    with pytest.raises(model_store.ModelsMissing, match="9 of 9"):
        model_store.require_models(tmp_path, model_store.load_manifest("docling"))


def test_download_models_is_idempotent(httpserver: HTTPServer, tmp_path: Path) -> None:
    manifest = model_store.load_manifest(
        "docling", manifest_for(httpserver, MODEL_FILES), allow_http=True
    )

    assert model_store.download_models(tmp_path, manifest) is True

    for path, content in MODEL_FILES.items():
        assert (tmp_path / path).read_bytes() == content
    assert (tmp_path / manifest.marker).is_file()
    assert model_store.missing_models(tmp_path, manifest) == []
    requests = len(httpserver.log)
    assert requests == 2

    assert model_store.download_models(tmp_path, manifest) is False  # the marker is there

    assert len(httpserver.log) == requests  # nothing downloaded again
    assert sorted(entry.name for entry in tmp_path.iterdir()) == [
        manifest.marker,
        "docling",
        "tessdata",
    ]


def test_wrong_sha256_fails_and_leaves_no_marker(httpserver: HTTPServer, tmp_path: Path) -> None:
    data = manifest_for(httpserver, MODEL_FILES, wrong="docling/layout/model.safetensors")
    manifest = model_store.load_manifest("docling", data, allow_http=True)

    failing = re.escape("docling/layout/model.safetensors")
    with pytest.raises(model_store.ModelDownloadFailed, match=failing):
        model_store.download_models(tmp_path, manifest)

    assert list(tmp_path.iterdir()) == []  # no marker, no file, no staging directory
    assert model_store.missing_models(tmp_path, manifest) == sorted(MODEL_FILES)


def test_a_new_manifest_replaces_the_old_files(httpserver: HTTPServer, tmp_path: Path) -> None:
    old = model_store.load_manifest(
        "docling", manifest_for(httpserver, MODEL_FILES), allow_http=True
    )
    model_store.download_models(tmp_path, old)
    httpserver.clear()
    newer = {"tessdata/deu.traineddata": b"better deu model" * 100}
    new = model_store.load_manifest("docling", manifest_for(httpserver, newer), allow_http=True)

    assert model_store.download_models(tmp_path, new) is True

    assert (tmp_path / "tessdata/deu.traineddata").read_bytes() == newer["tessdata/deu.traineddata"]
    assert (tmp_path / new.marker).is_file()
    assert not (tmp_path / old.marker).exists()


@pytest.mark.parametrize(
    "entry",
    [
        'path = "../etc/passwd"\nurl = "https://example.org/a"\nsha256 = "'
        + "0" * 64
        + '"\nsize = 1',
        'path = "/abs"\nurl = "https://example.org/a"\nsha256 = "' + "0" * 64 + '"\nsize = 1',
        'path = "tessdata/a"\nurl = "ftp://example.org/a"\nsha256 = "' + "0" * 64 + '"\nsize = 1',
        'path = "tessdata/a"\nurl = "http://example.org/a"\nsha256 = "' + "0" * 64 + '"\nsize = 1',
        'path = "tessdata/a"\nurl = "https://example.org/a"\nsha256 = "abc"\nsize = 1',
        'path = "tessdata/a"\nurl = "https://example.org/a"\nsha256 = "' + "0" * 64 + '"\nsize = 0',
    ],
    ids=["dotdot", "absolute", "scheme", "http", "hash", "size"],
)
def test_manifest_refuses_bad_entries(entry: str) -> None:
    with pytest.raises(ValueError, match="manifest"):
        model_store.load_manifest("docling", f"[[files]]\n{entry}\n".encode())


def test_a_trickling_mirror_fails_at_the_file_deadline(
    httpserver: HTTPServer, tmp_path: Path
) -> None:
    manifest = model_store.load_manifest(
        "docling", manifest_for(httpserver, MODEL_FILES), allow_http=True
    )
    now = [0.0]

    def clock() -> float:  # every look at the clock: ten minutes later
        now[0] += 600.0
        return now[0]

    with pytest.raises(model_store.ModelDownloadFailed, match="did not arrive within"):
        model_store.download_models(tmp_path, manifest, clock=clock)

    assert list(tmp_path.iterdir()) == []


def test_a_redirect_away_from_https_fails(tmp_path: Path) -> None:
    body = b"tessdata" * 10
    entry = (
        '[[files]]\npath = "tessdata/deu.traineddata"\nurl = "https://models.example.org/deu"\n'
        f'sha256 = "{hashlib.sha256(body).hexdigest()}"\nsize = {len(body)}\n'
    )
    manifest = model_store.load_manifest("docling", entry.encode())

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.scheme == "https":
            return httpx.Response(302, headers={"location": "http://mirror.example.org/deu"})
        return httpx.Response(200, content=body)

    with pytest.raises(model_store.ModelDownloadFailed, match="away from https"):
        model_store.download_models(tmp_path, manifest, transport=httpx.MockTransport(answer))

    assert list(tmp_path.iterdir()) == []


def test_the_file_deadline_grows_with_the_size() -> None:
    small, large = model_store.file_deadline_seconds(1), model_store.file_deadline_seconds(10**9)
    assert 60 <= small <= 300
    assert large > 3600  # a gigabyte at the slowest rate accepted (256 KiB/s)


def test_built_in_manifest_pins_its_files() -> None:
    manifest = model_store.load_manifest("docling")

    paths = {file.path for file in manifest.files}
    assert {"tessdata/deu.traineddata", "tessdata/eng.traineddata", "tessdata/osd.traineddata"} <= (
        paths
    )
    for file in manifest.files:
        assert file.url.startswith("https://")
        assert "/main/" not in file.url  # at a pinned commit
        assert "/master/" not in file.url
    assert manifest.marker.startswith(".complete-")


def mock_manifest(files: dict[str, bytes]) -> model_store.Manifest:
    entries = [
        f'[[files]]\npath = "{path}"\nurl = "https://models.example.org/{path}"\n'
        f'sha256 = "{hashlib.sha256(content).hexdigest()}"\nsize = {len(content)}\n'
        for path, content in files.items()
    ]
    return model_store.load_manifest("docling", "\n".join(entries).encode())


def other_container_done(root: Path, manifest: model_store.Manifest) -> None:
    """What another init container on the same volume left: the files and the marker."""
    for path, content in MODEL_FILES.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_bytes(content)
    (root / manifest.marker).write_text(f"{manifest.digest}\n", encoding="utf-8")


def test_a_volume_another_init_container_filled_meanwhile_is_kept(tmp_path: Path) -> None:
    """Two init containers on one ReadWriteMany volume: the one that finishes second finds the
    marker after its downloads, keeps the other's files and discards its own staging."""
    manifest = mock_manifest(MODEL_FILES)
    calls: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if len(calls) == len(MODEL_FILES):
            other_container_done(tmp_path, manifest)
        path = request.url.path.removeprefix("/")
        return httpx.Response(200, content=MODEL_FILES[path])

    assert (
        model_store.download_models(tmp_path, manifest, transport=httpx.MockTransport(answer))
        is False
    )

    assert sorted(entry.name for entry in tmp_path.iterdir()) == [
        manifest.marker,
        "docling",
        "tessdata",
    ]
    assert model_store.missing_models(tmp_path, manifest) == []


def test_a_failed_swap_is_a_download_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest = mock_manifest(MODEL_FILES)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=MODEL_FILES[request.url.path[1:]])
    )

    def busy(source: Path, target: Path, staging: Path) -> None:
        raise OSError(16, "Device or resource busy")

    monkeypatch.setattr(model_store, "_replace", busy)

    with pytest.raises(model_store.ModelDownloadFailed, match="OSError"):
        model_store.download_models(tmp_path, manifest, transport=transport)
    assert not (tmp_path / manifest.marker).exists()
    assert [entry.name for entry in tmp_path.iterdir() if entry.name.startswith(".")] == []


def test_a_swap_that_lost_to_another_init_container_succeeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The other container renamed its directories while this one swapped: its marker says the
    volume is complete, so the error is not a failure."""
    manifest = mock_manifest(MODEL_FILES)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=MODEL_FILES[request.url.path[1:]])
    )

    def raced(source: Path, target: Path, staging: Path) -> None:
        other_container_done(tmp_path, manifest)
        raise OSError(39, "Directory not empty")

    monkeypatch.setattr(model_store, "_replace", raced)

    assert model_store.download_models(tmp_path, manifest, transport=transport) is False
    assert model_store.missing_models(tmp_path, manifest) == []


# --- manifest-generic: the Docling manifest unchanged, the ASR manifest ---------------------------


def test_model_store_generic_docling_unchanged() -> None:
    """The Docling worker's manifest is the package-5 file under the same marker: a volume that
    package 5 filled is still complete."""
    data = resources.files("text_extraction.documents").joinpath("models.toml").read_bytes()

    manifest = model_store.load_manifest("docling")

    assert manifest == model_store.load_manifest("docling", data)
    assert manifest.digest == hashlib.sha256(data).hexdigest()
    assert manifest.marker == f".complete-{hashlib.sha256(data).hexdigest()[:16]}"
    assert len(manifest.files) == 9


def test_unknown_manifest_name_is_refused() -> None:
    with pytest.raises(ValueError, match="manifest"):
        model_store.load_manifest("whisper")


ASR_MODELS = get_args(Settings.model_fields["asr_model"].annotation)


def test_asr_manifest_carries_every_model_the_settings_accept() -> None:
    manifest = model_store.load_manifest("asr")

    tops = {file.path.split("/")[0] for file in manifest.files}
    assert tops == set(ASR_MODELS) == {"small", "large-v3-turbo"}
    for model in ASR_MODELS:
        names = {file.path.split("/", 1)[1] for file in manifest.only(model).files}
        # faster-whisper's files of a model (utils._MODELS' allow_patterns)
        assert {"model.bin", "config.json", "tokenizer.json"} <= names
        assert any(name.startswith("vocabulary.") for name in names)
    for file in manifest.files:
        # A fixed commit of the model's repository, never a branch.
        assert re.fullmatch(
            r"https://huggingface\.co/[\w.-]+/[\w.-]+/resolve/[0-9a-f]{40}/[\w.-]+", file.url
        ), file.url
    sizes = {model: sum(f.size for f in manifest.only(model).files) for model in ASR_MODELS}
    assert 480_000_000 < sizes["small"] < 500_000_000
    assert 1_600_000_000 < sizes["large-v3-turbo"] < 1_650_000_000


def test_only_one_model_has_a_marker_of_its_own() -> None:
    manifest = model_store.load_manifest("asr")

    small, large = manifest.only("small"), manifest.only("large-v3-turbo")

    assert {f.path.split("/")[0] for f in small.files} == {"small"}
    assert {f.path.split("/")[0] for f in large.files} == {"large-v3-turbo"}
    assert len({small.marker, large.marker, manifest.marker}) == 3
    assert (small.name, large.name) == ("asr", "asr")


def test_a_change_to_another_model_keeps_the_marker(httpserver: HTTPServer) -> None:
    """A new pin of ``small`` must not make a volume with ``large-v3-turbo`` download again."""
    files = {"a/model.bin": b"a" * 10, "b/model.bin": b"b" * 10}
    before = model_store.load_manifest("asr", manifest_for(httpserver, files), allow_http=True)
    httpserver.clear()
    changed = {**files, "b/model.bin": b"newer b" * 10}
    after = model_store.load_manifest("asr", manifest_for(httpserver, changed), allow_http=True)

    assert before.marker != after.marker
    assert before.only("a").marker == after.only("a").marker
    assert before.only("b").marker != after.only("b").marker


def test_only_an_unknown_model_is_refused() -> None:
    with pytest.raises(ValueError, match="no files"):
        model_store.load_manifest("asr").only("medium")


def test_download_fetches_only_the_chosen_model(httpserver: HTTPServer, tmp_path: Path) -> None:
    files = {"small/model.bin": b"s" * 100, "large-v3-turbo/model.bin": b"l" * 100}
    manifest = model_store.load_manifest("asr", manifest_for(httpserver, files), allow_http=True)
    chosen = manifest.only("large-v3-turbo")

    assert model_store.download_models(tmp_path, chosen) is True

    assert [entry[0].path for entry in httpserver.log] == ["/large-v3-turbo/model.bin"]
    assert sorted(entry.name for entry in tmp_path.iterdir()) == [chosen.marker, "large-v3-turbo"]
    assert model_store.missing_models(tmp_path, chosen) == []
    assert model_store.missing_models(tmp_path, manifest) == ["small/model.bin"]
