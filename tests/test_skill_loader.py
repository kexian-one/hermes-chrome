from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from agent.skill_loader import SkillRegistry, Skill


@pytest.fixture()
def skills_dir(tmp_path: Path) -> Path:
    skill_dir = tmp_path / "test-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        textwrap.dedent("""\
            ---
            name: test-skill
            description: A test skill for unit testing
            ---

            # Test Skill Body

            This is the body content.
        """),
        encoding="utf-8",
    )

    other_dir = tmp_path / "other-skill"
    other_dir.mkdir()
    (other_dir / "SKILL.md").write_text(
        textwrap.dedent("""\
            ---
            name: other-skill
            description: Another skill
            ---

            Other body.
        """),
        encoding="utf-8",
    )
    return tmp_path


def test_list_skills_returns_name_and_description_only(skills_dir: Path) -> None:
    registry = SkillRegistry(skills_dir)
    skills = registry.list_skills()
    assert len(skills) == 2
    names = {s.name for s in skills}
    assert "test-skill" in names
    assert "other-skill" in names
    for s in skills:
        assert s.body == ""


def test_list_skills_descriptions(skills_dir: Path) -> None:
    registry = SkillRegistry(skills_dir)
    by_name = {s.name: s for s in registry.list_skills()}
    assert by_name["test-skill"].description == "A test skill for unit testing"
    assert by_name["other-skill"].description == "Another skill"


def test_load_full_returns_body(skills_dir: Path) -> None:
    registry = SkillRegistry(skills_dir)
    skill = registry.load_full("test-skill")
    assert skill.name == "test-skill"
    assert skill.description == "A test skill for unit testing"
    assert "Test Skill Body" in skill.body
    assert skill.path.name == "SKILL.md"


def test_load_full_raises_for_unknown(skills_dir: Path) -> None:
    registry = SkillRegistry(skills_dir)
    with pytest.raises(KeyError):
        registry.load_full("nonexistent-skill")


def test_max_idle_minutes_default(tmp_path: Path) -> None:
    skill_dir = tmp_path / "no-idle"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        textwrap.dedent("""\
            ---
            name: no-idle
            description: Skill without max_idle_minutes
            ---

            Body.
        """),
        encoding="utf-8",
    )
    registry = SkillRegistry(tmp_path)
    skill = registry.load_full("no-idle")
    assert skill.max_idle_minutes == 10


def test_max_idle_minutes_custom(tmp_path: Path) -> None:
    skill_dir = tmp_path / "custom-idle"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        textwrap.dedent("""\
            ---
            name: custom-idle
            description: Skill with custom max_idle_minutes
            max_idle_minutes: 60
            ---

            Body.
        """),
        encoding="utf-8",
    )
    registry = SkillRegistry(tmp_path)
    skill = registry.load_full("custom-idle")
    assert skill.max_idle_minutes == 60


def test_max_idle_minutes_non_integer_fallback(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    import logging
    skill_dir = tmp_path / "bad-idle"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        textwrap.dedent("""\
            ---
            name: bad-idle
            description: Skill with bad max_idle_minutes
            max_idle_minutes: "not-a-number"
            ---

            Body.
        """),
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING, logger="skill_loader"):
        registry = SkillRegistry(tmp_path)
        skill = registry.load_full("bad-idle")
    assert skill.max_idle_minutes == 10
    assert any("not an integer" in r.message for r in caplog.records)


def test_requires_browser_mcp_default_true(skills_dir: Path) -> None:
    registry = SkillRegistry(skills_dir)
    skill = registry.load_full("test-skill")
    assert skill.requires_browser_mcp is True


def test_requires_browser_mcp_custom_false(tmp_path: Path) -> None:
    skill_dir = tmp_path / "api-only"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        textwrap.dedent("""\
            ---
            name: api-only
            description: Skill without browser MCP
            requires_browser_mcp: false
            ---

            Body.
        """),
        encoding="utf-8",
    )
    registry = SkillRegistry(tmp_path)
    skill = registry.load_full("api-only")
    assert skill.requires_browser_mcp is False
    assert registry.list_skills()[0].requires_browser_mcp is False


def test_real_ecom_skill() -> None:
    skills_dir = Path(__file__).parent.parent / "skills"
    if not skills_dir.exists():
        pytest.skip("skills/ directory not found")
    registry = SkillRegistry(skills_dir)
    names = {s.name for s in registry.list_skills()}
    assert "ecom-best-source" in names

    full = registry.load_full("ecom-best-source")
    assert len(full.body) > 100
    assert full.requires_browser_mcp is True
    assert "run_ecom_script" in full.body
    assert "jd_product.py" in full.body
    assert "静态结果只覆盖浏览器没拿到的字段" in full.body
