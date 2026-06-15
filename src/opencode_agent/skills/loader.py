"""Skill Loader Module

Discovers, parses, and manages YAML skill definitions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger("SkillLoader")


@dataclass
class Rule:
    """A single rule within a skill."""

    local_id: str
    namespace: str
    global_id: str
    name: str
    severity: str
    description: str
    check_points: list[str] = field(default_factory=list)
    examples: dict[str, str] = field(default_factory=dict)


@dataclass
class Skill:
    """A code review skill definition."""

    id: str
    name: str
    description: str
    version: str
    skill_type: str
    language: str
    tags: list[str]
    agent_role: str
    agent_expertise: list[str]
    workflow: list[str]
    rules: list[Rule]
    output_format: dict
    exclusions: list[str]
    source_path: Path


def load_skill_from_data(data: dict, source_path: Path) -> Skill:
    """Parse skill data from a dictionary."""
    # Support both wrapped (skill:) and flat top-level formats
    skill_data = data.get("skill", data) if isinstance(data, dict) else {}

    required = ["id", "name", "description", "version", "rules"]
    for field_name in required:
        if field_name not in skill_data:
            if field_name == "id" and "name" in skill_data:
                # Fallback: use 'name' as 'id' if 'id' is missing
                skill_data["id"] = skill_data["name"]
                continue
            raise ValueError(f"Skill {source_path} missing required field: {field_name}")

    agent_data = skill_data.get("agent", {}) or {}
    output_format = skill_data.get("output_format", {}) or {}

    rules = []
    seen_local_ids: set[str] = set()
    for rule_data in skill_data.get("rules", []):
        local_id = rule_data["id"]
        if local_id in seen_local_ids:
            raise ValueError(f"Duplicate rule id '{local_id}' in skill {source_path}")
        seen_local_ids.add(local_id)

        namespace = skill_data["id"]
        global_id = f"{namespace}:{local_id}"
        examples = rule_data.get("examples", {}) or {}
        rules.append(
            Rule(
                local_id=local_id,
                namespace=namespace,
                global_id=global_id,
                name=rule_data.get("name", local_id),
                severity=rule_data.get("severity", "MEDIUM"),
                description=rule_data.get("description", ""),
                check_points=rule_data.get("check_points", []) or [],
                examples=examples if isinstance(examples, dict) else {},
            )
        )

    return Skill(
        id=skill_data["id"],
        name=skill_data["name"],
        description=skill_data["description"],
        version=str(skill_data["version"]),
        skill_type=skill_data.get("type", "security-audit"),
        language=skill_data.get("language", "zh-CN"),
        tags=skill_data.get("tags", []) or [],
        agent_role=agent_data.get("role", ""),
        agent_expertise=agent_data.get("expertise", []) or [],
        workflow=skill_data.get("workflow", []) or [],
        rules=rules,
        output_format=output_format,
        exclusions=skill_data.get("exclusions", []) or [],
        source_path=source_path,
    )


def load_skill_from_path(path: Path) -> Skill:
    """Parse a single skill YAML file."""
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return load_skill_from_data(data, path)
