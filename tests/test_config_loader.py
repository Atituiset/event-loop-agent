"""Tests for config_loader module."""

import tempfile
from pathlib import Path

import yaml

from opencode_agent.core.config import AgentConfig, SkillConfig
from opencode_agent.skills.config_loader import ConfigLoader


def test_skill_config_defaults():
    cfg = SkillConfig()
    assert cfg.enabled is True
    assert cfg.rules == {}


def test_agent_config_defaults():
    cfg = AgentConfig()
    assert cfg.version == "1.0"
    assert cfg.skills == {}
    assert cfg.defaults == {}
    assert cfg.exclusions == []


def test_agent_config_with_skills():
    skill_cfg = SkillConfig(enabled=True, rules={"RULE-001": True})
    agent_cfg = AgentConfig(
        version="2.0",
        skills={"wireless-scan": skill_cfg},
        exclusions=["test/"],
    )
    assert agent_cfg.version == "2.0"
    assert "wireless-scan" in agent_cfg.skills
    assert agent_cfg.skills["wireless-scan"].rules["RULE-001"] is True
    assert agent_cfg.exclusions == ["test/"]


def test_global_config_loading():
    with tempfile.TemporaryDirectory() as tmp:
        config_dir = Path(tmp) / ".config" / "opencode"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                {
                    "skills": {
                        "wireless-code-scan": {
                            "enabled": True,
                            "rules": {"RULE-001": True, "RULE-005": False},
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        ConfigLoader.GLOBAL_CONFIG_PATH = config_file
        loader = ConfigLoader(project_dir=Path(tmp))

        assert loader.is_skill_enabled("wireless-code-scan") is True
        enabled = loader.get_enabled_rules(
            "wireless-code-scan", [f"RULE-{i:03d}" for i in range(1, 11)]
        )
        assert "RULE-001" in enabled
        assert "RULE-005" not in enabled


def test_project_override():
    with tempfile.TemporaryDirectory() as tmp:
        project_dir = Path(tmp)
        config_file = project_dir / ".opencode" / "skills.yaml"
        config_file.parent.mkdir(parents=True)
        config_file.write_text(
            yaml.dump(
                {
                    "skills": {
                        "wireless-code-scan": {
                            "enabled": False,
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        loader = ConfigLoader(project_dir=project_dir)
        assert loader.is_skill_enabled("wireless-code-scan") is False


def test_skill_boolean_enabled():
    with tempfile.TemporaryDirectory() as tmp:
        project_dir = Path(tmp)
        config_file = project_dir / ".opencode" / "skills.yaml"
        config_file.parent.mkdir(parents=True)
        config_file.write_text(
            yaml.dump(
                {
                    "skills": {
                        "memory-safety": True,
                        "cpp-modernization": False,
                    }
                }
            ),
            encoding="utf-8",
        )

        loader = ConfigLoader(project_dir=project_dir)
        assert loader.is_skill_enabled("memory-safety") is True
        assert loader.is_skill_enabled("cpp-modernization") is False


def test_default_skill_enabled():
    with tempfile.TemporaryDirectory() as tmp:
        loader = ConfigLoader(project_dir=Path(tmp))
        assert loader.is_skill_enabled("unknown-skill") is True
        assert loader.get_enabled_rules("unknown-skill", ["RULE-001"]) == ["RULE-001"]
