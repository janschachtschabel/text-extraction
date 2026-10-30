"""The external transcription backend (``media/asr_external.py``, ruling M11) against
pytest-httpserver as the OpenAI-compatible endpoint: chunks and their offsets, the
``verbose_json`` fallback, the key it sends, how the endpoint's answers map to errors, and that
the key is never in a log line or an exception. The decoding and the Opus encoding are fakes
here (PyAV is in the extra ``asr``: ``tests/asr`` encodes for real), so the suite runs without
the extra."""

import json
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import numpy as np
import pytest
from pytest_httpserver import HTTPServer
from werkzeug import Request, Response

from text_extraction.media import asr_external
from text_extraction.media.asr_external import (
    BudgetSpent,
    ExternalFailed,
    ExternalRejected,
    ExternalUnavailable,
    transcribe_external,
)

SR = 16_000
KEY = "sk-external-asr-key-7f3a9c"
PATH = "/srv/asr/item-x/upload.ogg"  # never opened: the decoding is a fake
ENDPOINT = "/v1/audio/transcriptions"
# Whisper's language codes (``faster_whisper.tokenizer._LANGUAGE_CODES`` in the child): a few.
WHISPER_CODES = frozenset({"de", "en", "fr", "haw"})


def tone(seconds: float, *, quiet_at: tuple[float, ...] = ()) -> np.ndarray[Any, Any]:
    """Constant sound with 0.1 s of silence ending at each of ``quiet_at``: a window ends
    there."""
    audio = np.full(int(seconds * SR), 0.1, dtype=np.float32)
    for second in quiet_at:
        audio[int((second - 0.1) * SR) : int(second * SR)] = 0.0
    return audio


def decoded(audio: np.ndarray[Any, Any]) -> Callable[[str], np.ndarray[Any, Any]]:
    return lambda _path: audio


def fake_encode(samples: np.ndarray[Any, Any]) -> bytes:
    return b"OggS" + str(len(samples)).encode()


class Endpoint:
    """The OpenAI-compatible endpoint: records each request (its form, file and headers) and
    answers with ``answer(index, request)``."""

    def __init__(
        self, server: HTTPServer, answer: Callable[[int, Request], Response] | None = None
    ) -> None:
        self.requests: list[dict[str, Any]] = []
        self.answer = answer or (lambda index, _request: verbose(index))
        self._lock = threading.Lock()
        server.expect_request(ENDPOINT, method="POST").respond_with_handler(self._handle)
        self.base_url = server.url_for("/v1")

    def _handle(self, request: Request) -> Response:
        with self._lock:
            index = len(self.requests)
            upload = request.files["file"]
            self.requests.append(
                {
                    "form": dict(request.form),
                    "file": (upload.filename, upload.mimetype, upload.read()),
                    "authorization": request.headers.get("Authorization"),
                    "user_agent": request.headers.get("User-Agent"),
                    "organization": request.headers.get("OpenAI-Organization"),
                    "project": request.headers.get("OpenAI-Project"),
                }
            )
        return self.answer(index, request)


def verbose(index: int, *, language: str = "de") -> Response:
    """A ``verbose_json`` answer: one segment from 1 s to 2 s of the chunk."""
    body = {
        "text": f"Chunk {index}.",
        "language": language,
        "duration": 1200.0,
        "segments": [{"id": 0, "start": 1.0, "end": 2.0, "text": f" Chunk {index}."}],
    }
    return Response(json.dumps(body), content_type="application/json")


def run(endpoint: Endpoint, audio: np.ndarray[Any, Any], **kwargs: Any) -> Any:
    values: dict[str, Any] = {
        "base_url": endpoint.base_url,
        "model": "whisper-1",
        "api_key": KEY,
        "timeout": 10.0,
        "languages": WHISPER_CODES,
        "decode": decoded(audio),
        "encode": fake_encode,
    }
    values.update(kwargs)
    return transcribe_external(PATH, **values)


def test_chunks_and_offsets(httpserver: HTTPServer) -> None:
    """45 minutes go out as three chunks of at most 20 minutes, cut in the pauses before 1200 s
    and 2400 s; each chunk's segments move by its start."""
    endpoint = Endpoint(httpserver)
    audio = tone(45 * 60, quiet_at=(1200.0, 2400.0))

    transcription = run(endpoint, audio)

    assert len(endpoint.requests) == 3
    sizes = [int(request["file"][2][4:]) for request in endpoint.requests]
    assert sum(sizes) == len(audio)
    assert all(size <= 1200 * SR for size in sizes)
    assert [request["file"][:2] for request in endpoint.requests] == [
        ("audio.ogg", "audio/ogg")
    ] * 3
    for request in endpoint.requests:
        assert request["form"]["model"] == "whisper-1"
        assert request["form"]["response_format"] == "verbose_json"
    starts = [cue.start for cue in transcription.segments]
    assert starts[0] == pytest.approx(1.0)
    assert starts[1] == pytest.approx(1200 + 1.0, abs=0.2)
    assert starts[2] == pytest.approx(2400 + 1.0, abs=0.2)
    assert [cue.text for cue in transcription.segments] == ["Chunk 0.", "Chunk 1.", "Chunk 2."]
    assert all(cue.end - cue.start == pytest.approx(1.0) for cue in transcription.segments)
    assert transcription.duration == pytest.approx(2700.0)
    assert transcription.language == "de"
    assert transcription.model == "whisper-1"


def test_the_requested_language_is_sent_and_kept(httpserver: HTTPServer) -> None:
    endpoint = Endpoint(httpserver, lambda index, _r: verbose(index, language="english"))

    transcription = run(endpoint, tone(30), language="de")

    assert endpoint.requests[0]["form"]["language"] == "de"
    assert transcription.language == "de"


def test_a_detected_language_carries_over_to_later_chunks(httpserver: HTTPServer) -> None:
    endpoint = Endpoint(httpserver)

    transcription = run(endpoint, tone(1500, quiet_at=(1200.0,)))

    assert "language" not in endpoint.requests[0]["form"]
    assert endpoint.requests[1]["form"]["language"] == "de"
    assert transcription.language == "de"


def test_a_chunk_without_speech_does_not_decide_the_language(httpserver: HTTPServer) -> None:
    def answer(index: int, _request: Request) -> Response:
        if index == 0:
            body = {"text": "", "language": "en", "segments": []}
            return Response(json.dumps(body), content_type="application/json")
        return verbose(index, language="de")

    endpoint = Endpoint(httpserver, answer)

    transcription = run(endpoint, tone(1500, quiet_at=(1200.0,)))

    assert "language" not in endpoint.requests[1]["form"]
    assert transcription.language == "de"


def test_a_language_name_is_no_code(httpserver: HTTPServer) -> None:
    """OpenAI's ``verbose_json`` names the language (``german``): no code is made up, the
    pipeline then reads the language from the text."""
    endpoint = Endpoint(httpserver, lambda index, _r: verbose(index, language="german"))

    transcription = run(endpoint, tone(1500, quiet_at=(1200.0,)))

    assert transcription.language == ""
    assert all("language" not in request["form"] for request in endpoint.requests)


def test_a_code_whisper_does_not_know_is_not_sent_on(httpserver: HTTPServer) -> None:
    """A detected code goes to the later chunks only when Whisper knows it; otherwise no
    language is sent and none is claimed."""
    endpoint = Endpoint(httpserver, lambda index, _r: verbose(index, language="xx"))

    transcription = run(endpoint, tone(1500, quiet_at=(1200.0,)))

    assert all("language" not in request["form"] for request in endpoint.requests)
    assert transcription.language == ""


def test_a_three_letter_code_whisper_knows_is_sent_on(httpserver: HTTPServer) -> None:
    endpoint = Endpoint(httpserver, lambda index, _r: verbose(index, language="haw"))

    transcription = run(endpoint, tone(1500, quiet_at=(1200.0,)))

    assert endpoint.requests[1]["form"]["language"] == "haw"
    assert transcription.language == "haw"


def test_verbose_json_fallback_to_json(httpserver: HTTPServer) -> None:
    """An endpoint that refuses ``verbose_json`` gets ``json`` for that chunk and the rest; a
    chunk then is one segment over its length."""

    def answer(_index: int, request: Request) -> Response:
        if request.form["response_format"] == "verbose_json":
            return Response('{"error": {"message": "unsupported"}}', status=400)
        return Response(json.dumps({"text": " Guten Tag. "}), content_type="application/json")

    endpoint = Endpoint(httpserver, answer)

    transcription = run(endpoint, tone(1500, quiet_at=(1200.0,)))

    formats = [request["form"]["response_format"] for request in endpoint.requests]
    assert formats == ["verbose_json", "json", "json"]
    first, second = transcription.segments
    assert (first.start, first.text) == (0.0, "Guten Tag.")
    assert first.end == pytest.approx(second.start)
    assert second.start == pytest.approx(1200.0, abs=0.2)
    assert second.end == pytest.approx(1500.0)


def test_verbose_json_without_segments_keeps_its_text(httpserver: HTTPServer) -> None:
    endpoint = Endpoint(
        httpserver,
        lambda _i, _r: Response(
            json.dumps({"text": "Hallo.", "segments": []}), content_type="application/json"
        ),
    )

    transcription = run(endpoint, tone(30))

    ((start, end, text),) = [(c.start, c.end, c.text) for c in transcription.segments]
    assert (start, end, text) == (0.0, pytest.approx(30.0), "Hallo.")


def test_silence_has_no_segments(httpserver: HTTPServer) -> None:
    endpoint = Endpoint(
        httpserver,
        lambda _i, _r: Response(
            json.dumps({"text": "", "segments": []}), content_type="application/json"
        ),
    )

    assert run(endpoint, tone(30)).segments == []


def test_header_key_wins(httpserver: HTTPServer) -> None:
    """The endpoint sees the key it is given (the worker's choice: the caller's header, else
    ``ASR_EXTERNAL_API_KEY``) as a bearer token, and our user agent."""
    endpoint = Endpoint(httpserver)

    run(endpoint, tone(30), api_key="caller-header-key", user_agent="text-extraction/0.1.0")

    (request,) = endpoint.requests
    assert request["authorization"] == "Bearer caller-header-key"
    assert request["user_agent"] == "text-extraction/0.1.0"


def test_openai_environment_does_not_reach_the_endpoint(
    httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #19: the SDK's organization and project from the environment are never sent, and
    an Authorization line of OPENAI_CUSTOM_HEADERS (refused at start-up anyway) does not replace
    the key."""
    monkeypatch.setenv("OPENAI_ORG_ID", "org-from-env")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "proj-from-env")
    monkeypatch.setenv("OPENAI_CUSTOM_HEADERS", "Authorization: Bearer sk-from-env")
    endpoint = Endpoint(httpserver)

    run(endpoint, tone(30), api_key="caller-header-key", user_agent="text-extraction/0.1.0")

    (request,) = endpoint.requests
    assert request["authorization"] == "Bearer caller-header-key"
    assert request["user_agent"] == "text-extraction/0.1.0"
    assert request["organization"] is None
    assert request["project"] is None


@pytest.mark.parametrize("status", [401, 403])
def test_401_not_retryable(httpserver: HTTPServer, status: int) -> None:
    endpoint = Endpoint(httpserver, lambda _i, _r: Response("no", status=status))

    with pytest.raises(ExternalRejected) as error:
        run(endpoint, tone(30))

    assert str(error.value) == str(status)
    assert len(endpoint.requests) == 1  # no retry


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_429_and_5xx_retryable(httpserver: HTTPServer, status: int) -> None:
    endpoint = Endpoint(httpserver, lambda _i, _r: Response("busy", status=status))

    with pytest.raises(ExternalUnavailable) as error:
        run(endpoint, tone(30))

    assert str(error.value) == str(status)
    assert len(endpoint.requests) == 1  # the SDK does not retry: the item's budget decides


def test_429_and_timeout_retryable(httpserver: HTTPServer) -> None:
    """A chunk that takes longer than ``ASR_EXTERNAL_TIMEOUT_SECONDS`` is retryable."""

    def slow(_index: int, _request: Request) -> Response:
        time.sleep(1.5)
        return verbose(0)

    endpoint = Endpoint(httpserver, slow)

    with pytest.raises(ExternalUnavailable) as error:
        run(endpoint, tone(30), timeout=0.5)

    assert str(error.value) == "timeout"


def trickle(_index: int, _request: Request) -> Response:
    """An answer that sends a byte every 0.2 s for 2 s: no read waits long, the whole does."""

    def body() -> Any:
        for _ in range(10):
            yield b" "
            time.sleep(0.2)
        yield json.dumps({"text": "late", "language": "de", "segments": []}).encode()

    return Response(body(), content_type="application/json")


@pytest.mark.parametrize(
    ("kwargs", "raised"),
    [
        ({"timeout": 0.6}, ExternalUnavailable),
        ({"timeout": 60.0, "budget": 1.6}, BudgetSpent),
    ],
    ids=["chunk-timeout", "item-budget"],
)
def test_a_trickling_answer_is_cut_at_the_chunks_total_bound(
    httpserver: HTTPServer, kwargs: dict[str, Any], raised: type[Exception]
) -> None:
    """The chunk's timeout bounds its whole request, not each read."""
    endpoint = Endpoint(httpserver, trickle)
    started = time.monotonic()

    with pytest.raises(raised):
        run(endpoint, tone(30), **kwargs)

    assert time.monotonic() - started < (2.0 if "budget" in kwargs else 1.5)


def test_a_timeout_cut_by_the_items_budget_is_the_budget(httpserver: HTTPServer) -> None:
    """When the item's budget (``ASR_TIMEOUT_SECONDS``, what is left of it) ends before the
    chunk's own timeout, the answer is the budget's."""

    def slow(_index: int, _request: Request) -> Response:
        time.sleep(2.5)
        return verbose(0)

    endpoint = Endpoint(httpserver, slow)

    with pytest.raises(BudgetSpent):
        run(endpoint, tone(30), timeout=60.0, budget=1.5)


def test_a_spent_budget_sends_nothing(httpserver: HTTPServer) -> None:
    endpoint = Endpoint(httpserver)

    with pytest.raises(BudgetSpent):
        run(endpoint, tone(30), budget=0.0)

    assert endpoint.requests == []


def test_an_unreachable_endpoint_is_retryable() -> None:
    with pytest.raises(ExternalUnavailable) as error:
        transcribe_external(
            PATH,
            base_url="http://127.0.0.1:9/v1",
            model="whisper-1",
            api_key=KEY,
            timeout=5.0,
            languages=WHISPER_CODES,
            decode=decoded(tone(30)),
            encode=fake_encode,
        )

    assert str(error.value) == "connection"


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (404, "not found"),
        (302, ""),
        (200, "not json"),
        (200, '["a list"]'),
        (200, '{"segments": "nonsense"}'),
    ],
    ids=["404", "redirect", "not-json", "list", "no-text"],
)
def test_other_answers_fail_for_good(httpserver: HTTPServer, status: int, body: str) -> None:
    headers = {"Location": "http://169.254.169.254/latest"} if status == 302 else {}
    endpoint = Endpoint(
        httpserver,
        lambda _i, _r: Response(body, status=status, headers=headers, content_type="text/plain"),
    )

    with pytest.raises(ExternalFailed):
        run(endpoint, tone(30))

    assert len(endpoint.requests) == 1  # a redirect is not followed


def test_key_never_logged(httpserver: HTTPServer, caplog: pytest.LogCaptureFixture) -> None:
    """Every logger at DEBUG (the SDK's, httpx2's), on success and on failures whose answer
    quotes the key: no log line and no exception text carries it."""
    caplog.set_level(logging.DEBUG)
    echo = json.dumps({"error": {"message": f"Incorrect API key provided: {KEY}"}})
    answers = iter(
        [
            verbose(0),
            Response(echo, status=401, content_type="application/json"),
            Response(echo, status=429, content_type="application/json"),
            Response(echo, status=400, content_type="application/json"),
            Response(echo, status=400, content_type="application/json"),
        ]
    )
    endpoint = Endpoint(httpserver, lambda _i, _r: next(answers))

    texts: list[str] = []
    run(endpoint, tone(30))
    for _ in range(3):
        with pytest.raises((ExternalRejected, ExternalUnavailable, ExternalFailed)) as error:
            run(endpoint, tone(30))
        texts.extend([str(error.value), repr(error.value)])
        # Raised outside the SDK's exception, so no traceback can show that one's text.
        assert error.value.__cause__ is None
        assert error.value.__context__ is None

    assert len(endpoint.requests) == 5
    assert all(KEY not in text for text in texts)
    assert caplog.records  # the libraries did log
    assert KEY not in caplog.text
    assert all(KEY not in str(record.__dict__) for record in caplog.records)


def test_the_client_neither_follows_redirects_nor_reads_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The endpoint is the operator's (no egress proxy): no proxy or credentials from the
    environment, no redirect to another host, no retries of the SDK."""
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://elsewhere.example/v1")

    client = asr_external._client(
        "https://asr.example.org/v1", KEY, timeout=30.0, user_agent="text-extraction/0.1.0"
    )

    try:
        assert str(client.base_url).rstrip("/") == "https://asr.example.org/v1"
        assert client.max_retries == 0
        http = client._client
        assert http.follow_redirects is False
        assert http.trust_env is False
    finally:
        client.close()
