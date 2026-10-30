"""The parts of the LLM side that need no OpenAI SDK: ``LlmError`` and ``truncate_input``.

``client.py`` re-exports both. Code that only marks LLM work as failed or not done (the post
stage without an LLM, ``structured/post.finish_post``) imports them from here, so that a worker
process that never calls the LLM (the ASR worker's parent, ``asr_worker.py``) does not load the
SDK.
"""

from text_extraction.envelope import ErrorCode


class LlmError(Exception):
    """An LLM call failed. ``message`` is safe to put into an answer: a fixed text that names
    the HTTP status at most. ``code`` is ``ErrorCode.LLM_FAILED``. ``status`` is the endpoint's
    HTTP status when it answered with one, else None (timeout, connection, an answer that does
    not fit); ``invalid_answer`` is true only for an answer that did not fit the schema after the
    repair call (ruling R-V1). With both, the vision step tells an image the endpoint refuses
    (400, 413, 422, or an invalid answer) from an endpoint that is down."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        code: ErrorCode = ErrorCode.LLM_FAILED,
        status: int | None = None,
        invalid_answer: bool = False,
    ):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.message = message
        self.status = status
        self.invalid_answer = invalid_answer


def truncate_input(text: str, max_chars: int) -> tuple[str, bool]:
    """``text`` cut to at most ``max_chars`` characters, and whether it was cut. The cut is at
    the last blank line before ``max_chars``, else the last line break, else the last space, else
    at ``max_chars``; the kept text has no trailing white space."""
    if len(text) <= max_chars:
        return text, False
    window = text[: max(max_chars, 0)]
    for separator in ("\n\n", "\n", " "):
        position = window.rfind(separator)
        if position > 0:
            return window[:position].rstrip(), True
    return window, True
