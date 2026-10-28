"""Fixtures of the PII tests: the spaCy models of the ``nlp`` tests (ruling R2)."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import cast, get_args

import pytest

from text_extraction import model_store
from text_extraction.pii.types import ModelSize, model_names


@dataclass(frozen=True)
class NlpModels:
    path: Path  # the models volume
    size: ModelSize


@pytest.fixture(scope="session")
def nlp_models() -> NlpModels:
    """The two spaCy models of ``PII_TEST_MODEL_SIZE`` (default ``md``) under
    ``PII_TEST_MODELS_PATH`` (default ``~/.cache/text-extraction/nlp-models``), downloaded with
    ``model_store.download_models`` when they are not there yet and kept for the next run. A
    volume may hold both sizes: a present model is not downloaded (nor checked) again."""
    size = os.environ.get("PII_TEST_MODEL_SIZE", "md")
    if size not in get_args(ModelSize):
        raise pytest.UsageError("PII_TEST_MODEL_SIZE must be lg or md")
    root = Path(
        os.environ.get("PII_TEST_MODELS_PATH", "~/.cache/text-extraction/nlp-models")
    ).expanduser()
    manifest = model_store.load_manifest("nlp").only(*model_names(cast(ModelSize, size)).values())
    if model_store.missing_models(root, manifest):
        model_store.download_models(root, manifest)
    return NlpModels(root, cast(ModelSize, size))
