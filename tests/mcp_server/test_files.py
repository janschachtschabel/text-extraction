"""Files over MCP (D3 a): ``decode_file`` and the options merge of the extraction tools."""

import base64

import pytest

from text_extraction.errors import ServiceError
from text_extraction.mcp_server.files import decode_file
from text_extraction.mcp_server.tools_extract import merge_options

DATA = bytes(range(256)) * 4


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def test_decode_file_forms() -> None:
    assert decode_file(b64(DATA), len(DATA)) == (DATA, None)
    assert decode_file(base64.encodebytes(DATA).decode(), len(DATA)) == (DATA, None)
    assert decode_file(" \t" + b64(DATA) + "\r\n", len(DATA)) == (DATA, None)
    assert decode_file("data:application/pdf;base64," + b64(DATA), len(DATA)) == (
        DATA,
        "application/pdf",
    )
    assert decode_file("DATA:text/plain;charset=utf-8;BASE64," + b64(b"hi"), 2) == (
        b"hi",
        "text/plain;charset=utf-8",
    )
    assert decode_file("data:;base64," + b64(b"hi"), 2) == (b"hi", None)
    assert decode_file("", 10) == (b"", None)


@pytest.mark.parametrize(
    "value",
    [
        "not base64!",
        b64(DATA)[:-1],
        b64(DATA)[:-4] + "a===",
        "data:application/pdf," + b64(DATA),
        "data:application/pdf;base64",
        "data:" + "a" * 256 + ";base64," + b64(DATA),
        b64(DATA).replace("A", "-"),
        "äöüß",
    ],
)
def test_decode_file_refuses_what_is_not_base64(value: str) -> None:
    with pytest.raises(ServiceError) as caught:
        decode_file(value, 10 * len(DATA))

    assert caught.value.code == "invalid_request"
    assert caught.value.message == "content_base64 is not valid base64."


@pytest.mark.parametrize("size", [1, 2, 3, 1000, 1001, 1002])
def test_decode_file_limit_exact(size: int) -> None:
    data = b"x" * size
    assert decode_file(b64(data), size) == (data, None)
    with pytest.raises(ServiceError) as caught:
        decode_file(b64(data), size - 1)
    assert caught.value.code == "invalid_request"
    assert caught.value.message == f"The file is larger than {size - 1} bytes."


def test_decode_file_refuses_large_before_decoding(monkeypatch: pytest.MonkeyPatch) -> None:
    def never(*args: object, **kwargs: object) -> bytes:
        raise AssertionError("a file over the limit must not be decoded")

    monkeypatch.setattr(base64, "b64decode", never)
    with pytest.raises(ServiceError):
        decode_file(b64(b"x" * 4097), 4096)


def test_merge_options() -> None:
    assert merge_options({"url": "u"}, None) == {"url": "u"}
    assert merge_options({"url": "u"}, {}) == {"url": "u"}
    assert merge_options({"url": "u"}, {"links": True}) == {"url": "u", "links": True}
    with pytest.raises(ServiceError) as caught:
        merge_options({"url": "u", "format": "text"}, {"format": "html", "url": "v"})
    assert caught.value.code == "invalid_request"
    assert caught.value.message == "format is given twice."
