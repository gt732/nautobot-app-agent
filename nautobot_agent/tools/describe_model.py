"""Metadata-only model description tool."""

from langchain.tools import tool
from pydantic import BaseModel, ConfigDict, StrictStr

from nautobot_agent.data.admission import describe, model_from_name
from nautobot_agent.data.tool_io import configure_tool_errors, dumps


class _DescribeModelInput(BaseModel):
    """Closed input schema for metadata-only model discovery."""

    model_config = ConfigDict(extra="forbid")

    model: StrictStr


@tool(args_schema=_DescribeModelInput)
def describe_model(model: str) -> str:
    """Discover stored fields and standard Django relationships for one registered model.

    Use relationship query_name for ORM paths and accessor for trace includes.
    Sensitive fields and internal-storage relationships are omitted; internal-storage
    models are unavailable. The data-admission policy may deny metadata for a blocked model;
    listing a relationship does not authorize retrieving its target's details. Metadata is not
    authorization; generic and other nonstandard relationships are omitted.
    """
    return dumps(describe(model_from_name(model)))


configure_tool_errors(describe_model)
