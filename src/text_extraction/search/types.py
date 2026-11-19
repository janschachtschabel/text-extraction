"""Request and answer models of the web search."""

from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

# The last result page a search may ask for.
MAX_PAGE: Final = 10


class SearchRequest(BaseModel):
    """What a caller asks. ``max_results`` against ``SEARCH_MAX_RESULTS`` and ``sources``
    against ``SEARCH_SOURCES`` are checked by the route; the bounds here are static."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=500, description="The search terms.")
    max_results: int = Field(default=10, ge=1, description="Most hits to return.")
    language: str | None = Field(
        default=None, min_length=2, max_length=10, description="Language of the hits (ISO 639-1)."
    )
    page: int = Field(default=1, ge=1, le=MAX_PAGE, description="Result page.")
    time_range: Literal["day", "month", "year"] | None = Field(
        default=None, description="Only hits from this period (where a source supports it)."
    )
    sources: list[str] | None = Field(
        default=None, description="SearXNG engines to ask; unset: all of `SEARCH_SOURCES`."
    )


class SearchHit(BaseModel):
    url: str
    title: str
    snippet: str | None = None
    sources: list[str] = Field(default_factory=list, description="Engines that found the hit.")
    published: str | None = None
    score: float | None = None


class SourceFailure(BaseModel):
    source: str
    reason: str


class SearchResult(BaseModel):
    query: str
    results: list[SearchHit] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    sources_failed: list[SourceFailure] = Field(default_factory=list)
    cache: Literal["hit", "miss", "bypass"] = "bypass"
