"""Configuration dataclasses for skills and agent behavior."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SkillConfig:
    """Per-skill configuration."""

    enabled: bool = True
    rules: dict[str, bool] = field(default_factory=dict)


@dataclass
class AgentConfig:
    """Merged agent configuration."""

    version: str = "1.0"
    skills: dict[str, SkillConfig] = field(default_factory=dict)
    defaults: dict[str, Any] = field(default_factory=dict)
    exclusions: list[str] = field(default_factory=list)
