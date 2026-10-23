from importlib.metadata import version

import text_extraction


def test_version_matches_package_metadata() -> None:
    assert text_extraction.__version__ == version("text-extraction")


def test_version_is_semver() -> None:
    parts = text_extraction.__version__.split(".")
    assert len(parts) == 3
    assert all(part.isdigit() for part in parts)
