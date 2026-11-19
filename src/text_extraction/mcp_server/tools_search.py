"""The tool ``web_search`` (package 25): ``POST /v1/search`` through its core ``run_search``
(ruling P10-S1).

Its arguments are the whole REST body (``SearchRequest``), so it has no ``options``. The call
guard counts the rate-limit hit and the gate checks the key; the core runs every other check of
the route (search switched off 400, ``SEARCH_ALLOW_USERS`` 403, the deployment's bounds 422).
The answer is the route's envelope, its hits cut to ``MCP_MAX_ANSWER_CHARS`` (``fit_answer``).
The tool is listed while search is off, as the job tools are while jobs are off; a call then
answers the route's 400.
"""

from mcp_types import ToolAnnotations
from pydantic import BaseModel, ConfigDict
from starlette.datastructures import State

from text_extraction.auth import Principal
from text_extraction.mcp_server.calls import ModelTool
from text_extraction.mcp_server.results import fit_answer, payload_result
from text_extraction.mcp_server.tools_extract import check_body_size, lean_schema
from text_extraction.search.types import SearchRequest
from text_extraction.search_api import default_max_results, run_search
from text_extraction.settings import Settings


class WebSearchArguments(SearchRequest):
    """The fields of ``POST /v1/search``'s body; unknown ones are refused, as in REST."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class WebSearchTool(ModelTool):
    """Search the web through the service's SearXNG (``POST /v1/search``)."""

    args_model = WebSearchArguments

    def __init__(self, settings: Settings) -> None:
        super().__init__(
            name="web_search",
            description="Search the web through the service's own search (POST /v1/search): "
            "each hit has url, title, snippet, the sources that found it and its date. Read a "
            "hit's text with extract_url, several hits with batch_extract. A search whose "
            "sources did not all answer is partial; one that failed has status failed and "
            "error.retryable. service_info tells whether the key may search (features.search).",
            parameters=lean_schema(WebSearchArguments),
            annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
        )
        # The deployment's bounds, so an agent need not learn them from a refusal.
        properties = self.parameters["properties"]
        properties["max_results"]["maximum"] = settings.search_max_results
        properties["max_results"]["default"] = default_max_results(settings)
        properties["sources"]["description"] = (
            f"Sources to ask, any of: {', '.join(settings.search_sources)}; leave out to ask "
            "all of them."
        )

    async def call(self, state: State, principal: Principal, args: BaseModel) -> object:
        assert isinstance(args, WebSearchArguments)  # noqa: S101 - narrows the type
        settings: Settings = state.settings
        # The route's body limit: a tool call may carry far more (``mcp_body_limit``).
        check_body_size(args.model_dump(exclude_unset=True), settings.request_max_body_bytes)
        envelope = await run_search(state, principal, args)
        payload = envelope.model_dump(mode="json")
        hints = fit_answer(payload, max_chars=settings.mcp_max_answer_chars)
        return payload_result(payload, is_error=False, hints=hints)
