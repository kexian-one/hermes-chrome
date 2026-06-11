from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

log = logging.getLogger("skill_loader")


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str
    path: Path
    max_idle_minutes: int = 10
    requires_browser_mcp: bool = True


def _parse_skill_md(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}, text
    end = text.index("---", 3)
    frontmatter = yaml.safe_load(text[3:end])
    body = text[end + 3:].lstrip("\n")
    return frontmatter, body


def _parse_max_idle_minutes(fm: dict) -> int:
    raw = fm.get("max_idle_minutes")
    if raw is None:
        return 10
    if isinstance(raw, int):
        return raw
    log.warning("max_idle_minutes %r is not an integer, falling back to 10", raw)
    return 10


def _parse_requires_browser_mcp(fm: dict) -> bool:
    raw = fm.get("requires_browser_mcp")
    if raw is None:
        return True
    if isinstance(raw, bool):
        return raw
    log.warning("requires_browser_mcp %r is not a boolean, falling back to true", raw)
    return True


class SkillRegistry:
    def __init__(self, skills_dir: Path) -> None:
        self._skills_dir = skills_dir
        self._meta: dict[str, tuple[str, str, Path, int, bool]] = {}
        self._scan()

    def _scan(self) -> None:
        for skill_md in self._skills_dir.glob("*/SKILL.md"):
            fm, _ = _parse_skill_md(skill_md)
            name = fm.get("name", skill_md.parent.name)
            description = fm.get("description", "")
            max_idle = _parse_max_idle_minutes(fm)
            requires_browser_mcp = _parse_requires_browser_mcp(fm)
            self._meta[name] = (name, description, skill_md, max_idle, requires_browser_mcp)

    def list_skills(self) -> list[Skill]:
        return [
            Skill(name=name, description=desc, body="", path=path, max_idle_minutes=max_idle)
            if requires_browser_mcp else
            Skill(
                name=name,
                description=desc,
                body="",
                path=path,
                max_idle_minutes=max_idle,
                requires_browser_mcp=False,
            )
            for name, desc, path, max_idle, requires_browser_mcp in self._meta.values()
        ]

    def load_full(self, name: str) -> Skill:
        if name not in self._meta:
            raise KeyError(f"skill {name!r} not found")
        _name, description, path, max_idle, requires_browser_mcp = self._meta[name]
        _, body = _parse_skill_md(path)
        return Skill(
            name=name,
            description=description,
            body=body,
            path=path,
            max_idle_minutes=max_idle,
            requires_browser_mcp=requires_browser_mcp,
        )
