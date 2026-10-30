"""The tool ``service_info`` (``GET /v1/info``)."""

from mcp_types import ToolAnnotations
from pydantic import BaseModel, ConfigDict
from starlette.datastructures import State

from text_extraction.app import service_info
from text_extraction.auth import Principal
from text_extraction.mcp_server.calls import ModelTool


class NoArguments(BaseModel):
    """A tool without arguments; any argument is refused, as REST refuses unknown fields."""

    model_config = ConfigDict(extra="forbid")


class ServiceInfoTool(ModelTool):
    """Name and version of the service, and who the caller is (``GET /v1/info``)."""

    args_model = NoArguments

    def __init__(self) -> None:
        super().__init__(
            name="service_info",
            description="Name and version of the service, and the caller's key name and role.",
            parameters=NoArguments.model_json_schema(),
            annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
        )

    async def call(self, state: State, principal: Principal, args: BaseModel) -> object:
        return service_info(principal)
