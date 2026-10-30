"""The core of ``GET /v1/info``: name and version of the service, and who the caller is. A module
of its own, so that the MCP tool ``service_info`` does not import the app module."""

from pydantic import BaseModel

from text_extraction import __version__
from text_extraction.auth import Principal, Role
from text_extraction.envelope import Envelope
from text_extraction.tracing import current_trace_id

SERVICE_NAME = "text-extraction"


class InfoData(BaseModel):
    name: str
    version: str
    principal: str
    role: Role


def service_info(principal: Principal) -> Envelope[InfoData]:
    """The core of ``GET /v1/info`` (ruling P10-S1): name and version of the service, and who
    the caller is.

    Caller's checks (this core runs none of them): ``principal_for`` (the user role, which every
    key has), then one ``check_rate`` hit."""
    data = InfoData(
        name=SERVICE_NAME,
        version=__version__,
        principal=principal.name,
        role=principal.role,
    )
    return Envelope(status="success", data=data, trace_id=current_trace_id())
