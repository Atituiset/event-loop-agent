"""Config Loader Module

Loads and merges global (~/.config/opencode/config.yaml) and project-specific
(.opencode/skills.yaml) YAML configuration for skill enable/disable management.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

import yaml

from opencode_agent.core.config import AgentConfig, SkillConfig

logger = logging.getLogger("ConfigLoader")


class ConfigLoader:
    """Loads and merges global and project-specific configuration."""

    GLOBAL_CONFIG_PATH = Path.home() / ".config" / "opencode" / "config.yaml"
    PROJECT_CONFIG_NAME = Path(".opencode") / "skills.yaml"

    def __init__(self, project_dir: Optional[Path] = None):
        self.project_dir = project_dir or Path.cwd()
        self.global_config: Optional[AgentConfig] = None
        self.project_config: Optional[AgentConfig] = None
        self._merged: AgentConfig = AgentConfig()
        self._load()

    def _load(self) -> None:
        """Load global and project configs."""
        if self.GLOBAL_CONFIG_PATH.exists():
            try:
                self.global_config = self._parse_config(self.GLOBAL_CONFIG_PATH)
                logger.debug(f"Loaded global config from {self.GLOBAL_CONFIG_PATH}")
            except Exception as e:
                logger.warning(f"Failed to load global config: {e}")

        project_config_path = self.project_dir / self.PROJECT_CONFIG_NAME
        if project_config_path.exists():
            try:
                self.project_config = self._parse_config(project_config_path)
                logger.debug(f"Loaded project config from {project_config_path}")
            except Exception as e:
                logger.warning(f"Failed to load project config: {e}")

        self._merged = self._merge_configs()

    def _parse_config(self, path: Path) -> AgentConfig:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if not isinstance(data, dict):
            data = {}

        skills: dict[str, SkillConfig] = {}
        raw_skills = data.get("skills", {}) or {}
        for skill_id, skill_data in raw_skills.items():
            if isinstance(skill_data, dict):
                rules: dict[str, bool] = {}
                if "rules" in skill_data and isinstance(skill_data["rules"], dict):
                    for rid, enabled in skill_data["rules"].items():
                        rules[str(rid)] = bool(enabled)
                skills[str(skill_id)] = SkillConfig(
                    enabled=bool(skill_data.get("enabled", True)),
                    rules=rules,
                )
            elif isinstance(skill_data, bool):
                skills[str(skill_id)] = SkillConfig(enabled=skill_data)

        return AgentConfig(
            version=str(data.get("version", "1.0")),
            skills=skills,
            defaults=dict(data.get("defaults", {}) or {}),
            exclusions=list(data.get("exclusions", []) or []),
        )

    def _merge_configs(self) -> AgentConfig:
        """Project config overrides global config."""
        base = AgentConfig()

        if self.global_config:
            base.skills.update(self.global_config.skills)
            base.defaults.update(self.global_config.defaults)
            base.exclusions.extend(self.global_config.exclusions)

        if self.project_config:
            base.skills.update(self.project_config.skills)
            base.defaults.update(self.project_config.defaults)
            base.exclusions.extend(self.project_config.exclusions)

        return base

    def is_skill_enabled(self, skill_id: str) -> bool:
        skill_cfg = self._merged.skills.get(skill_id)
        return skill_cfg.enabled if skill_cfg else True

    def get_enabled_rules(self, skill_id: str, all_rules: list[str]) -> list[str]:
        """Return list of enabled rule local IDs for a skill."""
        skill_cfg = self._merged.skills.get(skill_id)
        if not skill_cfg or not skill_cfg.rules:
            return all_rules

        enabled = []
        for rid in all_rules:
            is_enabled = skill_cfg.rules.get(rid, True)
            if is_enabled:
                enabled.append(rid)
        return enabled

    def get_exclusions(self) -> list[str]:
        return list(self._merged.exclusions)

    def get_default(self, key: str, default: Optional[Any] = None) -> Any:
        return self._merged.defaults.get(key, default)

    def list_enabled_skills(self, available_skills: list[str]) -> list[str]:
        """Filter available skills by enabled status."""
        return [sid for sid in available_skills if self.is_skill_enabled(sid)]

    def get_skill_config(self, skill_id: str) -> SkillConfig:
        return self._merged.skills.get(skill_id, SkillConfig())
