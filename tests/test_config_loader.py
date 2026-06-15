from opencode_agent.core.config import AgentConfig, SkillConfig


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
