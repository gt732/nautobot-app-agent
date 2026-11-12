"""Immutable skill definitions and packaged, app-maintained built-ins."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from importlib.resources import files

import yaml
from django.core.exceptions import ValidationError

BUILTIN_SKILL_IDENTIFIERS = ("nautobot-charts", "nautobot-mermaid")


@dataclass(frozen=True)
class SkillDefinition:
    """Immutable executable content captured for one graph lifetime."""

    identifier: str
    description: str
    instructions: str

    @staticmethod
    def _yaml_scalar(value: str) -> str:
        encoded = json.dumps(value, ensure_ascii=False)
        return re.sub(
            r"[\x7f-\x9f\u2028\u2029\ufeff\ufffe\uffff]",
            lambda match: f"\\u{ord(match.group()):04x}",
            encoded,
        )

    @property
    def document(self) -> str:
        """Return deterministic Agent Skills frontmatter followed by the unchanged body."""
        return (
            "---\n"
            f"name: {self._yaml_scalar(self.identifier)}\n"
            f"description: {self._yaml_scalar(self.description)}\n"
            f"---\n"
            f"{self.instructions}"
        )


def builtin_skill_definitions() -> tuple[SkillDefinition, ...]:
    """Read packaged content for the next graph snapshot, without database copies."""
    definitions = []
    root = files("nautobot_agent").joinpath("builtin_skills")
    for identifier in BUILTIN_SKILL_IDENTIFIERS:
        document = root.joinpath(identifier, "SKILL.md").read_text(encoding="utf-8")
        try:
            opening, frontmatter, body = document.split("---\n", 2)
            metadata = yaml.safe_load(frontmatter)
            if opening or not isinstance(metadata, dict) or not body.strip():
                raise ValueError("Invalid skill metadata or body")
            description = metadata.get("description")
            if (
                metadata.get("name") != identifier
                or not isinstance(description, str)
                or not description.strip()
                or len(description) > 1024
            ):
                raise ValueError("Invalid skill metadata or body")
        except (ValueError, yaml.YAMLError) as error:
            raise ValidationError(f"Invalid packaged skill: {identifier}.") from error
        definitions.append(SkillDefinition(identifier, metadata["description"], body))
    return tuple(definitions)
