"""Which strings of a result PII handling reads, applying its findings to them, and the guard.

N5 names the fields: the content, the title, the text of the metadata, the link texts and the
address of a ``mailto:`` or ``tel:`` link, the uploader of a media file and the name of an
uploaded file. URLs, dates, language codes and licences are not personal data of the page's
author and stay. The author names and the uploader are a person as a whole when ``PERSON`` is
asked for: the field is one ``PERSON`` span without the analyser (a bare name has no sentence
for it to read).

One walk over the result serves both jobs (``text_fields`` lists the strings, ``apply_pii``
rewrites them), so that the names and the order cannot drift apart. ``fail_closed`` is the
guard behind it all: a result that was asked to be processed and was not never leaves the
service with its text.

Pure and free of Presidio and spaCy.
"""

import dataclasses
from collections import Counter
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from text_extraction.convert import Link
from text_extraction.envelope import Envelope, ErrorCode, ErrorInfo
from text_extraction.extract_models import ExtractResult
from text_extraction.metadata import PageMetadata
from text_extraction.pii.html import HtmlText
from text_extraction.pii.replace import Replacer
from text_extraction.pii.spans import resolve
from text_extraction.pii.types import EntityType, PiiEntity, PiiOptions, PiiReport, Span
from text_extraction.tracing import current_trace_id

FAIL_CLOSED_MESSAGE = "PII handling did not run; the content is withheld."
TOO_LARGE_MESSAGE = "The text is too large for PII handling; the content is withheld."
# The request checks' 422 and the nlp worker's refusal of a draft (annotate takes no HTML).
ANNOTATE_HTML_MESSAGE = "annotate needs format markdown or text."
TRUNCATED_WARNING = "pii_entities_truncated"
_CONTACT_SCHEMES = ("mailto:", "tel:")


@dataclass(frozen=True)
class TextField:
    """One string of a result for the analyser. ``whole`` is the type when the whole string is
    one entity (an author name), which needs no analyser."""

    name: str
    text: str
    whole: EntityType | None = None


def text_fields(result: ExtractResult, entities: Collection[EntityType]) -> list[TextField]:
    """The strings of ``result`` that N5 processes, in a stable order (the content, the title,
    the metadata, the links, the media, the document); blank strings are left out. ``entities``
    decides which fields are ``whole``."""
    fields: list[TextField] = []

    def collect(field: TextField) -> list[Span]:
        fields.append(field)
        return []

    _Walk(entities, collect, None).result(result)
    return fields


def apply_pii(
    result: ExtractResult,
    options: PiiOptions,
    spans: Mapping[str, list[Span]],
    *,
    language: Literal["de", "en"],
    key: bytes,
    hash_scope: Literal["service", "result"],
    max_entities: int,
    entities: Collection[EntityType],
) -> tuple[ExtractResult, list[str]]:
    """``result`` with the entities in ``spans`` (the analyser's findings per field name)
    handled as ``options.mode`` says, ``pii`` set, and the warnings the caller adds.

    The analyser is not asked about the ``whole`` fields: they become one ``PERSON`` span here
    (score 1.0) and findings for them are ignored. The other fields' spans are resolved first
    (``spans.resolve``). ``entities`` is required: the effective entity types (the request's, or
    the operator's defaults), the same the caller gave ``text_fields`` to choose what to send
    to the analyser; with ``PERSON`` among them, authors and the uploader are whole persons.
    A mode that replaces also drops the comments and processing instructions of HTML content
    (R8). ``annotate`` leaves every string as it is and lists the entities, at most
    ``max_entities`` of them; it cannot take HTML. Status, word count, quality, language and
    fingerprint are the extraction's, and the caller sets ``timings.pii_ms``.
    """
    if options.mode == "annotate" and result.format == "html":
        raise ValueError("annotate needs format markdown or text")
    counts: Counter[EntityType] = Counter()
    items: list[PiiEntity] = []

    def handle(field: TextField) -> list[Span]:
        if field.whole is not None:
            stripped = field.text.strip()
            start = len(field.text) - len(field.text.lstrip())
            found = [Span(start, start + len(stripped), field.whole, 1.0)]
        else:
            found = resolve(spans.get(field.name, []), field.text)
        for span in found:
            counts[span.type] += 1
            if options.mode == "annotate" and len(items) < max_entities:
                items.append(
                    PiiEntity(
                        field=field.name,
                        type=span.type,
                        start=span.start,
                        end=span.end,
                        score=span.score,
                        text=field.text[span.start : span.end],
                    )
                )
        return found

    processed = _Walk(
        entities,
        handle,
        Replacer(options.mode, key),
        drop_comments=options.mode != "annotate",
    ).result(result)
    report = PiiReport(
        mode=options.mode,
        language=language,
        entities=dict(counts),
        items=items if options.mode == "annotate" else None,
        hash_scope=hash_scope if options.mode == "hash" else None,
    )
    truncated = options.mode == "annotate" and sum(counts.values()) > len(items)
    return processed.model_copy(update={"pii": report}), [TRUNCATED_WARNING] if truncated else []


def needs_pii(envelope: Envelope[ExtractResult], options: PiiOptions | None) -> bool:
    """Whether the envelope still owes the PII step: it was asked for, the envelope has data
    that is not processed yet, and the data has a string that N5 processes, or HTML content
    with a comment (which a replacing mode drops).

    Which fields exist does not depend on the entity types (only whether they are ``whole``), so
    this passes ``options.entities`` as given, and none when the request names none."""
    data = envelope.data
    if options is None or data is None or data.pii is not None:
        return False
    if text_fields(data, options.entities or ()):
        return True
    return bool(
        data.format == "html" and data.content and HtmlText.parse(data.content).has_comments
    )


def fail_closed(
    envelope: Envelope[ExtractResult], options: PiiOptions | None
) -> Envelope[ExtractResult]:
    """N12: an envelope that owes the PII step is answered as ``failed``/``pii_failed``
    (retryable), with the content, title, metadata, links, media uploader and file name
    withheld and the rest kept (what the target answered, compliance, timings). Any other
    envelope is returned as it is."""
    data = envelope.data
    if data is None or not needs_pii(envelope, options):
        return envelope
    withheld = data.model_copy(
        update={
            "content": None,
            "title": None,
            "metadata": None,
            "links": None,
            "media": data.media.model_copy(update={"uploader": None}) if data.media else None,
            "document": data.document.model_copy(update={"file_name": None})
            if data.document
            else None,
        }
    )
    return Envelope[ExtractResult](
        status="failed",
        data=withheld,
        error=ErrorInfo(code=ErrorCode.PII_FAILED, message=FAIL_CLOSED_MESSAGE, retryable=True),
        trace_id=envelope.trace_id or current_trace_id(),
    )


def withheld(
    envelope: Envelope[ExtractResult],
    options: PiiOptions | None,
    *,
    code: ErrorCode = ErrorCode.PII_FAILED,
    message: str = FAIL_CLOSED_MESSAGE,
    retryable: bool = True,
) -> Envelope[ExtractResult]:
    """``envelope`` withheld by the PII step: ``fail_closed`` (its text, metadata, links,
    uploader and file name removed, the facts of the target kept), then ``failed`` with
    ``code``, ``message`` and ``retryable``. For an envelope that owes the step; one that does
    not keeps its data and fails all the same."""
    held = fail_closed(envelope, options)
    error = ErrorInfo(code=code, message=message, retryable=retryable)
    return held.model_copy(update={"status": "failed", "error": error})


def withheld_by_pii(envelope: Envelope[ExtractResult]) -> bool:
    """Whether the PII step withheld the envelope's text (``withheld``): ``pii_failed``, or the
    nlp worker's ``too_large``. A target's own ``too_large`` has another message; the message
    is the one constant both ``nlp_worker`` and this check use."""
    error = envelope.error
    if error is None:
        return False
    if error.code == ErrorCode.PII_FAILED:
        return True
    return error.code == ErrorCode.TOO_LARGE and error.message == TOO_LARGE_MESSAGE


def _is_contact_url(url: str) -> bool:
    return url.lstrip().lower().startswith(_CONTACT_SCHEMES)


def _escaped_key(key: str) -> str:
    """A ``lrmi`` key for a dotted field name: its own dots and backslashes are escaped, so two
    different paths never share a name."""
    return key.replace("\\", "\\\\").replace(".", "\\.")


class _Walk:
    """One pass over the strings of a result. ``handle`` gets each field and returns its
    resolved spans; with a ``replacer`` the walk writes the replacements and returns the
    result with them, else the result comes back as it was."""

    def __init__(
        self,
        entities: Collection[EntityType],
        handle: Callable[[TextField], list[Span]],
        replacer: Replacer | None,
        *,
        drop_comments: bool = False,
    ) -> None:
        self._drop_comments = drop_comments
        self._person = "PERSON" in entities
        self._handle = handle
        self._replacer = replacer

    def text(self, name: str, text: str, *, person: bool = False) -> str:
        if not text.strip():
            return text
        whole: EntityType | None = "PERSON" if person and self._person else None
        spans = self._handle(TextField(name, text, whole))
        if not spans or self._replacer is None:
            return text
        return self._replacer.apply(text, spans)

    def optional(self, name: str, text: str | None, *, person: bool = False) -> str | None:
        return None if text is None else self.text(name, text, person=person)

    def html(self, name: str, html: str) -> str:
        """The HTML with its text handled. Under a replacing mode its comments and processing
        instructions go too (R8): they are not text, so no analyser reads them, and nothing
        unprocessed may leave. ``annotate`` leaves the markup as it is."""
        parsed = HtmlText.parse(html)
        spans = self._handle(TextField(name, parsed.text)) if parsed.text.strip() else []
        replacer = self._replacer
        if replacer is None or not self._drop_comments:
            return html
        replacements = [
            (span, replacer.placeholder(span, parsed.text[span.start : span.end])) for span in spans
        ]
        return parsed.render(replacements, drop_comments=True)

    def result(self, result: ExtractResult) -> ExtractResult:
        update: dict[str, Any] = {}
        if result.content is not None:
            update["content"] = (
                self.html("content", result.content)
                if result.format == "html"
                else self.text("content", result.content)
            )
        update["title"] = self.optional("title", result.title)
        if result.metadata is not None:
            update["metadata"] = self._metadata(result.metadata)
        if result.links is not None:
            update["links"] = [self._link(index, link) for index, link in enumerate(result.links)]
        if result.media is not None:
            uploader = self.optional("media.uploader", result.media.uploader, person=True)
            update["media"] = result.media.model_copy(update={"uploader": uploader})
        if result.document is not None:
            file_name = self.optional("document.file_name", result.document.file_name)
            update["document"] = result.document.model_copy(update={"file_name": file_name})
        return result.model_copy(update=update)

    def _metadata(self, metadata: PageMetadata) -> PageMetadata:
        return metadata.model_copy(
            update={
                "title": self.optional("metadata.title", metadata.title),
                "description": self.optional("metadata.description", metadata.description),
                "authors": [
                    self.text(f"metadata.authors[{index}]", author, person=True)
                    for index, author in enumerate(metadata.authors)
                ],
                "keywords": [
                    self.text(f"metadata.keywords[{index}]", keyword)
                    for index, keyword in enumerate(metadata.keywords)
                ],
                "site_name": self.optional("metadata.site_name", metadata.site_name),
                "images": [
                    image.model_copy(
                        update={"alt": self.text(f"metadata.images[{index}].alt", image.alt)}
                    )
                    for index, image in enumerate(metadata.images)
                ],
                "lrmi": self._lrmi("metadata.lrmi", metadata.lrmi),
            }
        )

    def _link(self, index: int, link: Link) -> Link:
        text = self.text(f"links[{index}].text", link.text)
        url = self.text(f"links[{index}].url", link.url) if _is_contact_url(link.url) else link.url
        return dataclasses.replace(link, text=text, url=url)

    def _lrmi(self, name: str, value: Any) -> Any:
        """``value`` with every string leaf handled; the leaf's field name is its dotted path
        (a list index is a plain segment: ``educationalAlignment.0.targetName``)."""
        if isinstance(value, str):
            return self.text(name, value)
        if isinstance(value, dict):
            return {
                key: self._lrmi(f"{name}.{_escaped_key(str(key))}", item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self._lrmi(f"{name}.{index}", item) for index, item in enumerate(value)]
        return value
