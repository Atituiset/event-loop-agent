"""Skill Registry Module

Registry of all loaded skills with namespaced rule lookup.
Supports both built-in skills (via importlib.resources) and project-specific skills.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import yaml

try:
    from importlib import resources as importlib_resources
except ImportError:
    import importlib_resources  # type: ignore[import-not-found]

from opencode_agent.skills.loader import load_skill_from_data, load_skill_from_path, Rule, Skill

logger = logging.getLogger("SkillRegistry")


class SkillRegistry:
    """Registry of all loaded skills with namespaced rule lookup."""

    def __init__(self, project_dir: Optional[Path] = None):
        self.skills: dict[str, Skill] = {}
        self.project_dir = project_dir
        self._load_all()

    def _load_all(self) -> None:
        """Load from builtin + project-specific directories."""
        # Built-in skills via importlib.resources
        try:
            builtin_package = "opencode_agent.skills"
            for resource_name in importlib_resources.files(builtin_package).iterdir():
                if resource_name.name.endswith(".yaml"):
                    try:
                        with resource_name.open("r", encoding="utf-8") as f:
                            data = yaml.safe_load(f)
                        skill_data = data.get("skill", data) if isinstance(data, dict) else {}
                        if "id" in skill_data or "name" in skill_data:
                            source_path = Path(f"<builtin>/{resource_name.name}")
                            skill = load_skill_from_data(data, source_path)
                            self.skills[skill.id] = skill
                            logger.debug(f"Loaded builtin skill: {skill.id} from {resource_name.name}")
                    except Exception as e:
                        logger.warning(f"Failed to load builtin skill {resource_name.name}: {e}")
        except Exception as e:
            logger.warning(f"Failed to load builtin skills: {e}")

        # Project-specific skills
        if self.project_dir:
            project_skills_dir = self.project_dir / ".opencode" / "skills"
            if project_skills_dir.exists():
                for yaml_file in sorted(project_skills_dir.glob("*.yaml")):
                    try:
                        skill = load_skill_from_path(yaml_file)
                        # Project skills override builtin if same ID
                        self.skills[skill.id] = skill
                        logger.debug(f"Loaded project skill: {skill.id} from {yaml_file}")
                    except Exception as e:
                        logger.warning(f"Failed to load project skill {yaml_file}: {e}")

    def get_skill(self, skill_id: str) -> Optional[Skill]:
        return self.skills.get(skill_id)

    def get_rule(self, global_id: str) -> Optional[Rule]:
        """Look up a rule by its global ID (namespace:local_id)."""
        if ":" not in global_id:
            return None
        namespace, local_id = global_id.split(":", 1)
        skill = self.skills.get(namespace)
        if not skill:
            return None
        for rule in skill.rules:
            if rule.local_id == local_id:
                return rule
        return None

    def list_rules(self, skill_id: Optional[str] = None) -> list[Rule]:
        """List all rules, optionally filtered by skill."""
        if skill_id:
            skill = self.skills.get(skill_id)
            return skill.rules if skill else []
        return [r for s in self.skills.values() for r in s.rules]

    def build_prompt_section(self, skill_id: str, enabled_rules: Optional[list[str]] = None) -> str:
        """Build the prompt text for a single skill's rules."""
        skill = self.skills.get(skill_id)
        if not skill:
            return ""

        rules_to_include = skill.rules
        if enabled_rules:
            enabled_set = set(enabled_rules)
            rules_to_include = [r for r in skill.rules if r.local_id in enabled_set]

        if not rules_to_include:
            return ""

        lines: list[str] = []
        lines.append(f"## {skill.name}")
        if skill.agent_role:
            lines.append(f"**角色**: {skill.agent_role}")

        if skill.agent_expertise:
            lines.append("")
            lines.append("**专长领域**:")
            for exp in skill.agent_expertise:
                lines.append(f"- {exp}")

        if skill.workflow:
            lines.append("")
            lines.append("**工作流程**:")
            for idx, step in enumerate(skill.workflow, 1):
                lines.append(f"{idx}. {step}")

        lines.append("")
        lines.append("### 扫描规则")
        for rule in rules_to_include:
            lines.append(f"#### {rule.global_id}: {rule.name} [{rule.severity}]")
            lines.append(rule.description)
            if rule.check_points:
                lines.append("")
                lines.append("**检查点**:")
                for cp in rule.check_points:
                    lines.append(f"- {cp}")
            if rule.examples.get("bad"):
                lines.append("")
                lines.append("**错误示例**:")
                lines.append("```c")
                lines.append(rule.examples["bad"].strip())
                lines.append("```")
            if rule.examples.get("good"):
                lines.append("")
                lines.append("**正确示例**:")
                lines.append("```c")
                lines.append(rule.examples["good"].strip())
                lines.append("```")
            lines.append("")

        return "\n".join(lines)

    def build_combined_prompt(
        self,
        skill_ids: list[str],
        enabled_rules_by_skill: Optional[dict[str, list[str]]] = None,
    ) -> str:
        """Build a combined prompt section from multiple skills."""
        enabled_rules_by_skill = enabled_rules_by_skill or {}
        sections: list[str] = []
        for skill_id in skill_ids:
            enabled_rules = enabled_rules_by_skill.get(skill_id)
            section = self.build_prompt_section(skill_id, enabled_rules)
            if section:
                sections.append(section)
        return "\n\n".join(sections)

    def list_enabled_global_ids(
        self,
        skill_ids: list[str],
        enabled_rules_by_skill: Optional[dict[str, list[str]]] = None,
    ) -> list[str]:
        """Return all enabled global rule IDs across the given skills."""
        enabled_rules_by_skill = enabled_rules_by_skill or {}
        result: list[str] = []
        for skill_id in skill_ids:
            skill = self.skills.get(skill_id)
            if not skill:
                continue
            enabled_rules = enabled_rules_by_skill.get(skill_id)
            if enabled_rules:
                enabled_set = set(enabled_rules)
                result.extend(r.global_id for r in skill.rules if r.local_id in enabled_set)
            else:
                result.extend(r.global_id for r in skill.rules)
        return result
