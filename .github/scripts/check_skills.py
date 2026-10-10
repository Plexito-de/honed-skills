"""Purpose: check every skill folder against the rules in CONTRIBUTING.md that a script can check.
Usage:   python3 .github/scripts/check_skills.py [repo root]   (exits 1 and lists every violation)

Checked: a SKILL.md with `name` and `description` frontmatter, `name` equal to the folder name, a
LICENSE in the folder, no `allowed-tools` key, no `!`command`` injection anywhere in SKILL.md, and no
binary asset inside the skill folder.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

BINARY = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".zip", ".mp4", ".mov", ".woff", ".woff2"}
# The `!`command`` form runs when an agent loads the skill. `$(...)` in a shell example does not.
SUBSTITUTION = re.compile(r"!`[^`]+`")


def frontmatter(text: str) -> dict[str, str] | None:
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end < 0:
        return None
    keys = {}
    for line in text[4:end].splitlines():
        if line and not line.startswith((" ", "\t")) and ":" in line:
            key, value = line.split(":", 1)
            keys[key.strip()] = value.strip()
    return keys


def check(root: Path) -> list[str]:
    problems: list[str] = []
    skills = sorted(p for p in (root / "skills").iterdir() if p.is_dir())
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
        if "allowed-tools" in meta:
            problems.append(f"{skill.name}: declares allowed-tools")
        body = text[text.find("\n---\n", 4) + 5 :]
        for line_no, line in enumerate(body.splitlines(), 1):
            if SUBSTITUTION.search(line):
                problems.append(f"{skill.name}: shell substitution in SKILL.md body line {line_no}")
        if not (skill / "LICENSE").is_file():
            problems.append(f"{skill.name}: no LICENSE in the skill folder")
        for f in skill.rglob("*"):
            if f.suffix.lower() in BINARY:
                problems.append(f"{skill.name}: binary asset {f.relative_to(skill)}; use docs/assets/")
    return problems


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[2]
    problems = check(root)
    for p in problems:
        print(p)
    print(f"checked {sum(1 for p in (root / 'skills').iterdir() if p.is_dir())} skills, "
          f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
