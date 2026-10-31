"""One-line texts of the result warnings for the UI's timeline (ruling P10-S17), from the
warnings that ``ExtractResult.warnings`` and ``docs/architecture.md`` describe. An unknown
warning (a converter note, a warning newer than this table) is shown by its name."""

import re
from typing import Final

WARNING_TEXTS: Final[dict[str, str]] = {
    # Browser mode.
    "browser_unavailable": "No browser worker took the page in time: this is the fast result.",
    "navigation_refused_non_get": "The browser refused a navigation that was not a GET.",
    "consent_overlay_removed": "A cookie or consent overlay was removed before reading.",
    "screenshot_clipped": "The screenshot was cut at the height limit.",
    "browser_bytes_capped": "The browser stopped loading at the byte limit.",
    "challenge_checkbox_clicked": "A challenge checkbox was clicked (unblock mode).",
    "challenge_not_cleared": "The bot challenge was not cleared.",
    "browser_profile_left": "The browser profile was released after the page.",
    # Documents.
    "ocr_unavailable": "The document needed OCR, which was not available: its text layer only.",
    "pages_truncated": "Pages after the page limit were cut.",
    "pages_partial": "OCR stopped at its time limit or skipped a page: the pages it read.",
    "document_fetched_directly": "The render ended on a file, which was fetched and converted.",
    # Media.
    "subtitles_blocked": "The platform refused the subtitles: the metadata answers.",
    "subtitles_failed": "The subtitles could not be read this time: the metadata answers.",
    "asr_unavailable": "No transcription in time, or it failed: the metadata answers.",
    "tracks_truncated": "More tracks than the limit: the first ones.",
    "tracks_failed": "No track of the page could be read.",
    "no_speech": "The transcription found no speech.",
    # Stored job items.
    "content_not_stored_too_large": "The job item was stored without its content (too large).",
    "chunks_not_stored_too_large": "The job item was stored without its chunks (too large).",
    "tables_not_stored_too_large": "The job item was stored without its tables (too large).",
    # PII.
    "pii_entities_truncated": "More personal data than the item limit: the counts are complete.",
    "pii_language_unsupported": "The language has no PII model: the fallback model was used.",
    # Structured output and chunking.
    "structured_value_invalid": "A value did not fit its field's type: the field is missing.",
    "pattern_timeout": "A pattern ran into its time limit: the next strategy looked it up.",
    "llm_unavailable": "The service has no LLM: the fields it would have read stay missing.",
    "llm_input_truncated": "The content was cut to the model's input limit for the LLM.",
    "learned_selectors_unavailable": "Learned selectors could not be read or stored this time.",
    "nothing_to_learn": "No field went to the LLM, so there was nothing to learn from.",
    "selector_learning_failed": "Learning selectors failed: the values stand, nothing learned.",
    "selectors_skipped_large_page": "The page has over 50,000 elements: no selector rules ran.",
    "selectors_timed_out": "The selector rules did not finish in time: their fields are missing.",
    "selectors_failed": "The selector rules could not run: their fields are missing.",
    "structured_truncated": "A selector found more than a field's limit: the first values.",
    "tables_truncated": "The tables are over 1,000,000 characters: the first ones.",
    "chunks_truncated": "More chunks than CHUNK_MAX_CHUNKS: the first ones.",
    "chunking_fallback_recursive": "The chunking strategy could not run: recursive was used.",
    # Image descriptions.
    "image_not_described": "An image was skipped or not described: its description is empty.",
    # Site structure.
    "sitemap_not_found": "A guessed /sitemap.xml does not exist.",
    "sitemap_truncated": "The sitemap file was over its size limit: the entries before the cut.",
    "sitemap_urls_truncated": "The crawl reached SITE_MAX_SITEMAP_URLS: the first entries.",
    "sitemap_files_truncated": "More sitemap files than SITE_MAX_SITEMAP_FILES: the first ones.",
    "links_truncated": "More links than SITE_MAX_LINKS_PER_PAGE: the first ones.",
    "max_pages_reached": "Links of the page were not followed: the crawl has max_pages pages.",
    "site_memory_guard": "Valkey was nearly full: no link of the page was followed.",
    # MCP tool answers.
    "mcp_content_truncated": "The MCP answer holds part of the content: ask for content_offset.",
}

_ACTION_FAILED: Final = re.compile(r"action_(\d+)_failed")


def warning_text(name: str) -> str | None:
    """The one-line text of the warning ``name``; None for a warning this table does not
    know (it is shown by its name)."""
    action = _ACTION_FAILED.fullmatch(name)
    if action is not None:
        return f"Browser action {action[1]} failed."
    return WARNING_TEXTS.get(name)
