"""The MCP endpoint (ruling P10-S2): a stateless FastMCP server served at exactly ``/mcp`` on the
public port, behind ``McpGate``.

``fastmcp`` is imported only inside ``build_mcp``, which the public app calls only with
``MCP_ENABLED``.
"""

import logging
import math
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from prometheus_client import CollectorRegistry
from starlette.datastructures import State
from starlette.types import ASGIApp

from text_extraction import __version__
from text_extraction.file_api import upload_file_limit
from text_extraction.settings import Settings

if TYPE_CHECKING:
    from fastmcp import FastMCP

PATH = "/mcp"
SERVER_NAME = "text-extraction"
SERVER_INSTRUCTIONS = (
    "Turns web pages, files, audio and video into clean text or Markdown. "
    "Use extract_url for pages and documents at a URL. "
    "A result whose data has a status_url is a job: poll get_job with its job_id and "
    "wait_seconds until it is completed. "
    "The options argument takes any further field of the REST body of the tool's route "
    "(see /docs)."
)
# FastMCP's import gives its logger a rich text handler on stderr that does not propagate; the
# service logs JSON on stdout through the root logger. Below WARNING, FastMCP logs tool arguments
# (DEBUG), which no log line may carry, as `configure_logging` also sets.
_FASTMCP_LOGGER = "fastmcp"


@dataclass(frozen=True)
class McpMount:
    """The MCP app for the gate, the context manager that the public lifespan enters (it runs the
    stateless session manager), and the FastMCP server (its tools)."""

    app: ASGIApp
    lifespan: Callable[[], AbstractAsyncContextManager[None]]
    server: "FastMCP[Any]"


def mcp_body_limit(settings: Settings) -> int:
    """The body limit of ``/mcp``: a file of the upload limit as base64 plus 64 KiB (D3 a)."""
    return math.ceil(upload_file_limit(settings) / 3) * 4 + 65_536


def _log_through_root() -> None:
    logger = logging.getLogger(_FASTMCP_LOGGER)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    logger.setLevel(max(logging.WARNING, logging.getLogger().getEffectiveLevel()))
    logger.propagate = True


def build_mcp(state: State, settings: Settings, registry: CollectorRegistry) -> McpMount:
    """The MCP server of the public app whose state is ``state``, with its tools and the call
    guard; its metrics are collected in ``registry``."""
    from fastmcp import FastMCP  # only with MCP_ENABLED

    from text_extraction.mcp_server.calls import (
        CallGuard,
        ModelTool,
        ToolMetrics,
        register,
    )
    from text_extraction.mcp_server.tools_extract import extraction_tools
    from text_extraction.mcp_server.tools_info import ServiceInfoTool

    _log_through_root()
    metrics = ToolMetrics(registry)
    server: FastMCP[Any] = FastMCP(
        name=SERVER_NAME,
        version=__version__,
        instructions=SERVER_INSTRUCTIONS,
        mask_error_details=True,
        middleware=[CallGuard(state, metrics)],
    )
    tools: list[ModelTool] = [*extraction_tools(settings), ServiceInfoTool()]
    for tool in tools:
        register(server, state, tool)
    metrics.export(tool.name for tool in tools)
    # The gate checks origins (P10-S3); FastMCP's own host and origin guard stays off.
    http = server.http_app(
        path=PATH, stateless_http=True, json_response=True, host_origin_protection=False
    )

    @asynccontextmanager
    async def lifespan() -> AsyncIterator[None]:
        async with http.router.lifespan_context(http):
            yield

    return McpMount(app=http, lifespan=lifespan, server=server)
