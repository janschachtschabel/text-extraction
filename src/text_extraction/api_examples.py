"""Request examples of the OpenAPI document for the extraction and batch routes (C13).

``tests/test_app.py`` validates each against its request model, so an example never shows a
body the service refuses.
"""

from typing import Any, Final

_ARTICLE = "https://www.example.org/news/article.html"

EXTRACT_EXAMPLES: Final[dict[str, Any]] = {
    "page": {
        "summary": "A page as Markdown, fast mode first and the browser when it needs one",
        "value": {"url": _ARTICLE},
    },
    "browser": {
        "summary": "A page that needs the browser: accept the cookie banner, scroll twice",
        "value": {
            "url": "https://www.example.org/app/",
            "mode": "browser",
            "actions": [
                {"type": "click", "selector": "#accept-cookies"},
                {"type": "scroll", "times": 2},
            ],
            "wait_seconds": 25,
        },
    },
    "structured": {
        "summary": "Text plus fields from the page's metadata and links",
        "value": {
            "url": _ARTICLE,
            "format": "text",
            "links": True,
            "structured": {
                "strategy": "metadata",
                "schema": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "author": {"type": "string"},
                        "published": {"type": "string"},
                    },
                },
            },
        },
    },
}

SCREENSHOT_EXAMPLES: Final[dict[str, Any]] = {
    "page": {
        "summary": "The whole page as a PNG",
        "value": {"url": _ARTICLE, "full_page": True},
    },
    "after_wait": {
        "summary": "The first screen once the main content shows, as a job after 30 s",
        "value": {
            "url": "https://www.example.org/app/",
            "actions": [{"type": "wait_for", "selector": "main"}],
            "wait_seconds": 30,
        },
    },
}

BATCH_EXAMPLES: Final[dict[str, Any]] = {
    "pages": {
        "summary": "Several pages as Markdown",
        "value": {
            "urls": [_ARTICLE, "https://www.example.org/news/other.html"],
        },
    },
    "text_and_links": {
        "summary": "Plain text and links of pages that need the browser",
        "value": {
            "urls": ["https://www.example.org/app/", "https://www.example.com/start/"],
            "mode": "browser",
            "format": "text",
            "links": True,
        },
    },
}

# The form of ``POST /v1/extract/file``; ``file`` stands for the uploaded part.
FILE_EXAMPLES: Final[dict[str, Any]] = {
    "document": {
        "summary": "A PDF or Office file as Markdown",
        "value": {"file": "report.pdf", "format": "markdown"},
    },
    "scan": {
        "summary": "A scanned PDF as text, read by OCR on every page",
        "value": {"file": "scan.pdf", "format": "text", "ocr": "always"},
    },
}
