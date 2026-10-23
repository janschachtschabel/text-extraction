"""FastAPI service for extracting text from documents."""

from importlib.metadata import version

__version__: str = version("text-extraction")

__all__ = ["__version__"]
