"""Model-visible query schemas, type stubs, description formatting and system-message addition.

Adapted from ``langchain-monty`` (``middleware/_tool_rendering.py`` and the
description formatting in ``middleware/monty_code_interpreter_middleware.py``),
MIT License, Copyright (c) 2026 Shane Smithrand; see the ``LICENSE`` file in
this directory.

Visible-field discovery lives here so schemas, stubs and the bridge's positional
argument normalization all see the same fields. Nothing in this module invokes a
tool or touches the interpreter.
"""

from typing import Any, cast

from langchain_core.messages import ContentBlock, SystemMessage
from langchain_core.tools import BaseTool
from pydantic import BaseModel

from nautobot_agent.app_settings import InterpreterLimits

# Parameters the host supplies from the trusted runtime; never model- or sandbox-visible.
INJECTED_PARAM_NAMES: frozenset[str] = frozenset(
    {"runtime", "state", "store", "tool_call_id", "config", "context", "stream_writer"}
)

# JSON Schema type -> Python annotation. Anything unmapped degrades to ``Any`` so
# stubs only ever under-constrain and never reject a call the tool would accept.
_JSON_TYPE_TO_PY: dict[str, str] = {
    "string": "str",
    "integer": "int",
    "number": "float",
    "boolean": "bool",
    "array": "list[Any]",
    "object": "dict[str, Any]",
    "null": "None",
}


def _call_schema(tool: BaseTool) -> dict[str, Any]:
    """Return the JSON schema the model sees for ``tool`` (injected arguments already removed)."""
    schema = tool.tool_call_schema
    return schema if isinstance(schema, dict) else cast(type[BaseModel], schema).model_json_schema()


def visible_parameters(tool: BaseTool) -> list[tuple[str, dict[str, Any], bool]]:
    """Return ``(name, property_schema, required)`` for each visible argument in declaration order."""
    schema = _call_schema(tool)
    required = set(schema.get("required", ()))
    return [
        (name, prop, name in required)
        for name, prop in schema.get("properties", {}).items()
        if name not in INJECTED_PARAM_NAMES
    ]


def visible_fields(tool: BaseTool) -> list[str]:
    """Return visible argument names in declaration order: the positional slots of a sandbox call."""
    return [name for name, _, _ in visible_parameters(tool)]


def _members(prop: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Return the union members of a property schema, or ``None`` when it is not a union."""
    members = prop.get("anyOf") or prop.get("oneOf")
    if members:
        return members
    if isinstance(prop.get("type"), list):
        return [{"type": member} for member in prop["type"]]
    return None


def _labels(prop: dict[str, Any]) -> list[str]:
    """Return the distinct human-readable type names for a property schema."""
    members = _members(prop)
    if members is not None:
        return list(dict.fromkeys(label for member in members for label in _labels(member)))
    if "$ref" in prop:
        return [prop["$ref"].rsplit("/", 1)[-1]]
    if isinstance(prop.get("type"), str):
        return [prop["type"]]
    return ["any"]


def _annotation(prop: dict[str, Any]) -> str:
    """Return the Python annotation for a property schema, ``Any`` when any member is unconstrained."""
    members = _members(prop)
    if members is not None:
        annotations = list(dict.fromkeys(_annotation(member) for member in members))
        return "Any" if "Any" in annotations else " | ".join(annotations)
    json_type = prop.get("type")
    return _JSON_TYPE_TO_PY.get(json_type, "Any") if isinstance(json_type, str) else "Any"


def format_tool_schema(tool: BaseTool) -> str:
    """Format a tool's signature, description and visible parameters for the system prompt."""
    params = visible_parameters(tool)
    signature = ", ".join(
        f"{name}: {' | '.join(_labels(prop))}" + ("" if required else " = ...") for name, prop, required in params
    )
    lines = [f"{tool.name}({signature})"]
    description = (tool.description or "").strip()
    if description:
        lines.extend(f"  {line}" for line in description.splitlines())
    if params and description:
        lines.append("  Parameters:")
        for name, prop, required in params:
            entry = f"    {name} ({' | '.join(_labels(prop))}, {'required' if required else 'optional'})"
            if prop.get("description"):
                entry += f" — {prop['description']}"
            lines.append(entry)
    return "\n".join(lines)


def render_type_stubs(tool: BaseTool) -> str:
    """Render the Python stub for ``tool`` consumed by Monty's static type checker."""
    if not tool.name.isidentifier():
        raise ValueError(f"Tool name {tool.name!r} cannot be called from sandbox code.")
    parts: list[str] = []
    defaulted = False
    for name, prop, required in visible_parameters(tool):
        if not name.isidentifier():
            # One unspellable name invalidates the whole stub; degrade to permissive.
            parts = ["*args: Any", "**kwargs: Any"]
            break
        # Python forbids a required parameter after a defaulted one; keep the call valid.
        defaulted = defaulted or not required
        parts.append(f"{name}: {_annotation(prop)}" + (" = ..." if defaulted else ""))
    return f"from typing import Any\n\ndef {tool.name}({', '.join(parts)}) -> Any: ...\n"


def _format_limit(value: float | int | None) -> float | int | str:
    return "unlimited" if value is None else value


def format_description(template: str, tool: BaseTool, limits: InterpreterLimits) -> str:
    """Resolve ``{available_host_tools}``, ``{max_duration_secs}`` and ``{max_memory_bytes}``.

    The description lists only the tool name; full signatures stay in the system prompt.
    """
    return template.format(
        available_host_tools=f"{tool.name}\n(full signatures are in the system prompt)",
        max_duration_secs=_format_limit(limits.max_duration_secs),
        max_memory_bytes=_format_limit(limits.max_memory_bytes),
    )


def append_to_system_message(system_message: SystemMessage | None, text: str) -> SystemMessage:
    """Append a text block to a (possibly absent) system message, preserving existing blocks."""
    blocks: list[ContentBlock] = list(system_message.content_blocks) if system_message else []
    if blocks:
        text = f"\n\n{text}"
    blocks.append({"type": "text", "text": text})
    return SystemMessage(content_blocks=blocks)
