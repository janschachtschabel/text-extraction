"""Shared fakes of the image-description tests: a fake ``ImageSource``, image bytes, results
with page images or an image upload, and an OpenAI-compatible endpoint (pytest-httpserver) that
answers vision calls and extraction calls apart."""

import asyncio
import json
from collections.abc import Callable
from typing import Any

from pytest_httpserver import HTTPServer
from werkzeug import Request, Response

from tests.structured.post_helpers import CHAT, completion, result
from text_extraction.compliance import ComplianceMode
from text_extraction.envelope import Envelope
from text_extraction.extract_models import ExtractResult
from text_extraction.metadata import Image, PageMetadata
from text_extraction.vision.fetch import ImageBytes, ImageSkipped

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
TIFF = b"II*\x00" + b"\x00" * 64


def image_url(number: int) -> str:
    return f"https://cdn.example.org/img/{number}.png"


class FakeImages:
    """An ``ImageSource`` that answers fixed outcomes per URL (default: a PNG), after ``delay``
    seconds for the URLs in ``slow``; it records the calls (URL, mode, timeout)."""

    def __init__(
        self,
        outcomes: dict[str, ImageBytes | ImageSkipped | BaseException] | None = None,
        *,
        slow: dict[str, float] | None = None,
    ) -> None:
        self.outcomes = outcomes or {}
        self.slow = slow or {}
        self.calls: list[tuple[str, ComplianceMode, float]] = []

    async def get(
        self,
        url: str,
        mode: ComplianceMode,
        *,
        timeout: float,  # noqa: ASYNC109 - the protocol's
    ) -> ImageBytes | ImageSkipped:
        self.calls.append((url, mode, timeout))
        if url in self.slow:
            await asyncio.sleep(self.slow[url])
        outcome = self.outcomes.get(url, ImageBytes(data=PNG, media_type="image/png"))
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def page_envelope(
    count: int,
    *,
    content: str = "# Fractions\n\nA page about fractions.",
    alts: list[str] | None = None,
    language: str | None = "de",
    result_language: str | None = "en",
    **fields: Any,
) -> Envelope[ExtractResult]:
    """A page result with ``count`` images in ``metadata.images``."""
    images = [
        Image(url=image_url(number), alt=(alts[number] if alts else f"Bild {number}"))
        for number in range(count)
    ]
    data = result(content, **fields).model_copy(
        update={
            "metadata": PageMetadata(language=language, images=images),
            "language": result_language,
        }
    )
    return Envelope[ExtractResult](status="success", data=data)


def upload_envelope(
    content: str = "Bruch 3/4",
    *,
    content_type: str = "image/png",
    format: str = "markdown",
) -> Envelope[ExtractResult]:
    """An image upload's result (OCR text ``content``)."""
    data = result(content, url=None, content_type=content_type, format=format).model_copy(
        update={"metadata": PageMetadata()}
    )
    return Envelope[ExtractResult](status="success", data=data)


def described(text: str | None) -> dict[str, Any]:
    return completion(json.dumps({"description": text}))


def is_vision(body: dict[str, Any]) -> bool:
    return bool(body["response_format"]["json_schema"]["name"] == "image_description")


def serve_llm(
    server: HTTPServer,
    vision: Callable[[dict[str, Any], int], dict[str, Any] | Response],
    *,
    extraction: dict[str, Any] | None = None,
) -> None:
    """The endpoint: ``vision(body, n)`` answers the n-th vision call (from 0), ``extraction``
    every other chat call."""
    seen = {"vision": 0}

    def handle(request: Request) -> Response:
        body = request.get_json()
        if is_vision(body):
            number = seen["vision"]
            seen["vision"] += 1
            reply = vision(body, number)
        else:
            reply = extraction or completion("{}")
        if isinstance(reply, Response):
            return reply
        return Response(json.dumps(reply), content_type="application/json")

    server.expect_request(CHAT, method="POST").respond_with_handler(handle)


def vision_requests(server: HTTPServer) -> list[dict[str, Any]]:
    bodies = [request.get_json() for request, _ in server.log if request.path == CHAT]
    return [body for body in bodies if is_vision(body)]


def user_text(body: dict[str, Any]) -> str:
    """The text part of a vision call's user message."""
    parts = body["messages"][1]["content"]
    return str(next(part["text"] for part in parts if part["type"] == "text"))
