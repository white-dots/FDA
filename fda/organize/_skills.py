# fda/organize/_skills.py
"""Load a SKILL.md file: parse YAML frontmatter, return (model, prompt, tools).

Tools are optional in the frontmatter and currently unused by every callsite —
all classifier skills are pure text-in / JSON-out. The signature carries
`tools` anyway so future skills can add tool surfaces without renaming.

YAML parsing is deliberately tiny: only the keys we use are supported (top-
level `name: value` pairs). We avoid pulling pyyaml as a new dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SkillConfig:
    name: str
    model: str
    description: str
    body: str
    tools: tuple[dict[str, Any], ...] = ()


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        raise ValueError("SKILL.md must start with YAML frontmatter '---' fence")
    end = text.find("\n---", 4)
    if end == -1:
        raise ValueError("SKILL.md frontmatter is not closed by '---'")
    raw = text[4:end]
    body = text[end + len("\n---"):].lstrip("\n")
    fields: dict[str, str] = {}
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip().strip('"').strip("'")
    return fields, body


def load_skill(skill_dir: Path) -> SkillConfig:
    """Load `<skill_dir>/SKILL.md`."""
    md = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    fields, body = _parse_frontmatter(md)
    name = fields.get("name") or skill_dir.name
    model = fields.get("model")
    if not model:
        raise ValueError(f"SKILL.md at {skill_dir} missing required 'model' field")
    description = fields.get("description", "")
    return SkillConfig(name=name, model=model, description=description, body=body)
