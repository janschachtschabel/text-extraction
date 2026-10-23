"""API service that extracts text and Markdown from web pages, files, audio and video."""

from importlib.metadata import version

__version__: str = version("text-extraction")

__all__ = ["__version__"]
