"""Package and core skills: how an agent learns each package.

A package's skill is ``skill/SKILL.md`` inside its package directory, plus
optional ``skill/references/<ref>.md`` files.  The core skill is
``src/facetmedia/skill/SKILL.md``, shipped in the wheel as package data.
Nothing is ever written to an agent directory: ``facet skill`` prints the
right skill from the registry in use and adds what is true on this machine.
"""

from __future__ import annotations

import re
from importlib import resources
from pathlib import Path
from typing import Final

from facetmedia.registry_types import Problem

__all__ = [
    "REQUIRED_HEADINGS",
    "SKILL_REF_RE",
    "core_skill_text",
    "parse_skill",
    "skill_description",
    "skill_references",
    "skill_text",
    "validate_skill",
]

SKILL_REF_RE: Final[re.Pattern[str]] = re.compile(r"[a-z0-9][a-z0-9-]*\Z")

#: The skill front matter's `description:` bounds: one line, 20 to 200 characters.
MIN_DESCRIPTION_LEN: Final[int] = 20
MAX_DESCRIPTION_LEN: Final[int] = 200

REQUIRED_HEADINGS: Final[tuple[str, ...]] = (
    "## When to use",
    "## Pieces",
    "## Judging the result",
    "## Pitfalls",
)

_HEADING_RE: Final[re.Pattern[str]] = re.compile(r"^##\s+(.*)$")


def parse_skill(text: str) -> tuple[dict[str, str], str] | None:
    """Split ``SKILL.md`` into its front matter and body, or ``None``.

    The front matter is a block between two ``---`` lines holding exactly
    the single-line keys ``name:`` and ``description:``.  No YAML dependency:
    each line is split on its first colon.
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end = lines.index("---", 1)
    except ValueError:
        return None
    meta: dict[str, str] = {}
    for raw in lines[1:end]:
        if not raw.strip():
            continue
        key, sep, value = raw.partition(":")
        if not sep:
            return None
        meta[key.strip()] = value.strip()
    if sorted(meta) != ["description", "name"]:
        return None
    return meta, "\n".join(lines[end + 1 :])


def _headings(body: str) -> list[str]:
    found: list[str] = []
    for line in body.splitlines():
        match = _HEADING_RE.match(line.rstrip())
        if match:
            found.append(f"## {match.group(1).strip()}")
    return found


def validate_skill(
    package_dir: Path, package_name: str, capability_ids: tuple[str, ...]
) -> list[Problem]:
    """Every skill problem of one package, never raising."""
    base = f"packages/{package_name}"
    path = package_dir / "skill" / "SKILL.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return [Problem(f"{base}/skill/SKILL.md", "skill", "missing")]
    found: list[Problem] = []
    parsed = parse_skill(text)
    if parsed is None:
        return [Problem(f"{base}/skill/SKILL.md", "skill.frontmatter", "value")]
    meta, body = parsed
    if meta["name"] != package_name:
        found.append(Problem(f"{base}/skill/SKILL.md", "skill.name", "value"))
    description = meta["description"]
    if not MIN_DESCRIPTION_LEN <= len(description) <= MAX_DESCRIPTION_LEN or "\n" in description:
        found.append(Problem(f"{base}/skill/SKILL.md", "skill.description", "value"))
    headings = _headings(body)
    for heading in REQUIRED_HEADINGS:
        if heading not in headings:
            found.append(Problem(f"{base}/skill/SKILL.md", "skill.headings", f"missing {heading}"))
    if all(heading in headings for heading in REQUIRED_HEADINGS):
        ordered = [heading for heading in headings if heading in REQUIRED_HEADINGS]
        if ordered != list(REQUIRED_HEADINGS):
            found.append(Problem(f"{base}/skill/SKILL.md", "skill.headings", "order"))
    for cap_id in sorted(capability_ids):
        if cap_id not in body:
            found.append(Problem(f"{base}/skill/SKILL.md", f"skill.capability.{cap_id}", "missing"))
    references_dir = package_dir / "skill" / "references"
    if references_dir.is_dir():
        for entry in sorted(references_dir.iterdir()):
            if entry.is_file():
                stem = entry.name[:-3] if entry.name.endswith(".md") else ""
                if not stem or not SKILL_REF_RE.fullmatch(stem):
                    found.append(
                        Problem(
                            f"{base}/skill/references/{entry.name}",
                            "skill.reference",
                            "value",
                        )
                    )
    return found


def skill_text(package_dir: Path) -> str | None:
    """The raw ``SKILL.md`` of a package, or ``None`` when unreadable."""
    try:
        return (package_dir / "skill" / "SKILL.md").read_text(encoding="utf-8")
    except OSError:
        return None


def skill_references(package_dir: Path) -> list[str]:
    """The sorted reference names under ``skill/references/``."""
    references_dir = package_dir / "skill" / "references"
    if not references_dir.is_dir():
        return []
    names: list[str] = []
    for entry in sorted(references_dir.iterdir()):
        if entry.is_file() and entry.name.endswith(".md"):
            stem = entry.name[:-3]
            if SKILL_REF_RE.fullmatch(stem):
                names.append(stem)
    return names


def skill_description(text: str) -> str:
    """The skill's one-line ``description:`` front matter, or ``""``."""
    parsed = parse_skill(text)
    if parsed is None:
        return ""
    return parsed[0].get("description", "")


def core_skill_text() -> str:
    """The core skill text shipped in the wheel as package data."""
    ref = resources.files("facetmedia") / "skill" / "SKILL.md"
    return ref.read_text(encoding="utf-8")
