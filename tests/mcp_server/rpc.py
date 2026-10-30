"""JSON-RPC over the MCP endpoint, as an MCP client posts it."""

from collections.abc import Mapping
from typing import Any

from fastapi.testclient import TestClient

ACCEPT = "application/json, text/event-stream"


def post(
    client: TestClient,
    method: str,
    params: Mapping[str, Any] | None = None,
    *,
    headers: Mapping[str, str] | None = None,
    path: str = "/mcp",
) -> Any:
    """POST one JSON-RPC request to ``path`` and return the HTTP response."""
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        message["params"] = dict(params)
    return client.post(
        path,
        json=message,
        headers={"Accept": ACCEPT, **(headers or {})},
        follow_redirects=False,
    )


def rpc(
    client: TestClient,
    method: str,
    params: Mapping[str, Any] | None = None,
    *,
    headers: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The JSON-RPC answer of one request to ``/mcp`` (HTTP 200 asserted)."""
    response = post(client, method, params, headers=headers)
    assert response.status_code == 200, response.text
    answer: dict[str, Any] = response.json()
    return answer


def call_tool(
    client: TestClient,
    name: str,
    arguments: Mapping[str, Any] | None = None,
    *,
    headers: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The ``result`` of a ``tools/call`` (a JSON-RPC error fails the test)."""
    answer = rpc(
        client, "tools/call", {"name": name, "arguments": dict(arguments or {})}, headers=headers
    )
    assert "result" in answer, answer
    result: dict[str, Any] = answer["result"]
    return result
