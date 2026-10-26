"""Purpose: check every skill folder against the rules in CONTRIBUTING.md that a script can check.
Usage:   python3 .github/scripts/check_skills.py [repo root]   (exits 1 and lists every violation)

Checked: a SKILL.md with `name` and `description` frontmatter, `name` equal to the folder name, a
`description` of at most 1,024 characters (the Agent Skills specification's limit; claude.ai refuses a
longer one on upload), a LICENSE in the folder, no `allowed-tools` key, no `!`command`` injection
anywhere in SKILL.md, no binary asset inside the skill folder, and
.github/ISSUE_TEMPLATE/misbehaves.yml options matching skills/.

Warned, not failed: a SKILL.md over 500 lines, the specification's recommendation for the main file.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

BINARY = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".zip", ".mp4", ".mov", ".woff", ".woff2"}
IGNORED_DIRS = {"node_modules", "__pycache__", ".git", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
# The `!`command`` form runs when an agent loads the skill. `$(...)` in a shell example does not.
SUBSTITUTION = re.compile(r"!`[^`]+`")
DESCRIPTION_MAX = 1024
LINES_MAX = 500


def is_ignored(rel_path: Path) -> bool:
    """True when a path sits inside an ignored dependency, venv or cache directory."""
    return any(
        part in IGNORED_DIRS or part.startswith((".venv", "venv"))
        for part in rel_path.parts[:-1]
    )


def frontmatter(text: str) -> dict[str, str] | None:
    """Top-level keys. Indented lines belong to the key above them, folded with spaces: a `>` or `|`
    block scalar (three skills here write `description: >-`, whose first line alone is two
    characters) and a plain or quoted scalar continued on the next lines alike."""
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end < 0:
        return None
    keys: dict[str, str] = {}
    last: str | None = None
    for line in text[4:end].splitlines():
        if line and not line.startswith((" ", "\t")) and ":" in line:
            key, value = line.split(":", 1)
            last, value = key.strip(), value.strip()
            keys[last] = "" if value[:1] in (">", "|") else value
        elif last and line.strip():
            keys[last] = f"{keys[last]} {line.strip()}".strip()
    return keys


def template_options(text: str) -> list[str]:
    """Extract options from the skill dropdown in an issue template."""
    m_block = re.search(r"id:\s*skill\b(.*?)(?:\n\s*-\s+type:|\Z)", text, re.DOTALL)
    section = m_block.group(1) if m_block else text
    match = re.search(r"options:\s*\[(.*?)\]", section, re.DOTALL)
    if match:
        return [item.strip().strip("'\"") for item in match.group(1).split(",") if item.strip()]
    items: list[str] = []
    in_options = False
    for line in section.splitlines():
        if re.match(r"^\s*options:\s*$", line):
            in_options = True
            continue
        if in_options:
            if not line.strip():
                continue
            m = re.match(r"^\s*-\s+(.*)$", line)
            if m:
                items.append(m.group(1).strip().strip("'\""))
            else:
                break
    return items


def check(root: Path) -> list[str]:
    problems: list[str] = []
    skills = sorted(p for p in (root / "skills").iterdir() if p.is_dir() and not p.name.startswith("."))
    if not skills:
        return ["no skill folders found under skills/"]
    for skill in skills:
        md = skill / "SKILL.md"
        if not md.is_file():
            problems.append(f"{skill.name}: no SKILL.md")
            continue
        text = md.read_text(encoding="utf-8")
        meta = frontmatter(text)
        if meta is None:
            problems.append(f"{skill.name}: SKILL.md has no frontmatter")
            continue
        for key in ("name", "description"):
            if not meta.get(key):
                problems.append(f"{skill.name}: frontmatter has no {key}")
        if meta.get("name") and meta["name"] != skill.name:
            problems.append(f"{skill.name}: frontmatter name is {meta['name']!r}")
        if len(meta.get("description", "")) > DESCRIPTION_MAX:
            problems.append(f"{skill.name}: description is {len(meta['description'])} characters, "
                            f"over the specification's 1,024")
        if "allowed-tools" in meta:
            problems.append(f"{skill.name}: declares allowed-tools")
        body = text[text.find("\n---\n", 4) + 5 :]
        for line_no, line in enumerate(body.splitlines(), 1):
            if SUBSTITUTION.search(line):
                problems.append(f"{skill.name}: shell substitution in SKILL.md body line {line_no}")
        if not (skill / "LICENSE").is_file():
            problems.append(f"{skill.name}: no LICENSE in the skill folder")
        for f in skill.rglob("*"):
            if not f.is_file():
                continue
            rel = f.relative_to(skill)
            if is_ignored(rel):
                continue
            if f.suffix.lower() in BINARY:
                problems.append(f"{skill.name}: binary asset {rel}; use docs/assets/")
    template = root / ".github" / "ISSUE_TEMPLATE" / "misbehaves.yml"
    if not template.is_file():
        problems.append(".github/ISSUE_TEMPLATE/misbehaves.yml: no misbehaves.yml template")
    else:
        actual_options = template_options(template.read_text(encoding="utf-8"))
        expected_skills = [p.name for p in skills]
        if actual_options != expected_skills:
            missing = sorted(set(expected_skills) - set(actual_options))
            extra = sorted(set(actual_options) - set(expected_skills))
            diff: list[str] = []
            if missing:
                diff.append(f"missing: {', '.join(missing)}")
            if extra:
                diff.append(f"extra: {', '.join(extra)}")
            if not missing and not extra:
                diff.append("order or duplicates do not match")
            problems.append(
                f".github/ISSUE_TEMPLATE/misbehaves.yml: options do not match skills/ ({'; '.join(diff)})"
            )
    return problems


def warn(root: Path) -> list[str]:
    """What the specification recommends rather than requires: reported, never a failure."""
    warnings = []
    for md in sorted((root / "skills").glob("*/SKILL.md")):
        lines = md.read_text(encoding="utf-8").count("\n")
        if lines > LINES_MAX:
            warnings.append(f"{md.parent.name}: SKILL.md is {lines} lines, over the recommended {LINES_MAX} lines")
    return warnings


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[2]
    problems = check(root)
    for w in warn(root):
        print(f"warning: {w}")
    for p in problems:
        print(p)
    print(f"checked {sum(1 for p in (root / 'skills').iterdir() if p.is_dir() and not p.name.startswith('.'))} skills, "
          f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
