"""Tests for skill_loader module."""

import tempfile
from pathlib import Path

import pytest
import yaml

from opencode_agent.skills.loader import Rule, Skill, load_skill_from_path
from opencode_agent.skills.registry import SkillRegistry


def test_load_builtin_skills():
    registry = SkillRegistry()
    assert "wireless-code-scan" in registry.skills
    skill = registry.skills["wireless-code-scan"]
    assert len(skill.rules) == 10
    assert skill.rules[0].global_id == "wireless-code-scan:RULE-001"


def test_namespaced_rule_lookup():
    registry = SkillRegistry()
    rule = registry.get_rule("wireless-code-scan:RULE-001")
    assert rule is not None
    assert rule.name == "TLV解析边界检查"
    assert rule.severity == "CRITICAL"


def test_project_skill_override():
    with tempfile.TemporaryDirectory() as tmp:
        project_dir = Path(tmp)
        skills_dir = project_dir / ".opencode" / "skills"
        skills_dir.mkdir(parents=True)

        skill_yaml = skills_dir / "wireless-code-scan.yaml"
        skill_yaml.write_text(
            yaml.dump(
                {
                    "skill": {
                        "id": "wireless-code-scan",
                        "name": "Overridden",
                        "description": "Test",
                        "version": "2.0.0",
                        "rules": [
                            {
                                "id": "RULE-001",
                                "name": "Test Rule",
                                "severity": "LOW",
                                "description": "Test desc",
                            }
                        ],
                    }
                }
            ),
            encoding="utf-8",
        )

        registry = SkillRegistry(project_dir=project_dir)
        skill = registry.skills["wireless-code-scan"]
        assert skill.version == "2.0.0"
        assert len(skill.rules) == 1


def test_build_prompt_section():
    registry = SkillRegistry()
    section = registry.build_prompt_section("wireless-code-scan", ["RULE-001", "RULE-002"])
    assert "TLV解析边界检查" in section
    assert "结构体强转内存安全" in section
    assert "RULE-003" not in section


def test_build_combined_prompt():
    registry = SkillRegistry()
    text = registry.build_combined_prompt(
        ["wireless-code-scan"],
        {"wireless-code-scan": ["RULE-001"]},
    )
    assert "wireless-code-scan" in text
    assert "RULE-001" in text


def test_list_enabled_global_ids():
    registry = SkillRegistry()
    ids = registry.list_enabled_global_ids(
        ["wireless-code-scan"],
        {"wireless-code-scan": ["RULE-001", "RULE-002"]},
    )
    assert "wireless-code-scan:RULE-001" in ids
    assert "wireless-code-scan:RULE-002" in ids
    assert "wireless-code-scan:RULE-003" not in ids


def test_flat_format_skill():
    """Support skills without 'skill:' wrapper."""
    with tempfile.TemporaryDirectory() as tmp:
        project_dir = Path(tmp)
        skills_dir = project_dir / ".opencode" / "skills"
        skills_dir.mkdir(parents=True)

        skill_yaml = skills_dir / "flat-skill.yaml"
        skill_yaml.write_text(
            yaml.dump(
                {
                    "id": "flat-skill",
                    "name": "Flat Skill",
                    "description": "Test",
                    "version": "1.0.0",
                    "rules": [
                        {
                            "id": "FLAT-001",
                            "name": "Flat Rule",
                            "severity": "MEDIUM",
                            "description": "Test",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        registry = SkillRegistry(project_dir=project_dir)
        assert "flat-skill" in registry.skills
        assert registry.skills["flat-skill"].rules[0].global_id == "flat-skill:FLAT-001"
