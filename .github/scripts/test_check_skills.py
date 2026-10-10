"""Purpose: prove check_skills.py fails on each rule it claims to check.
Usage:   python3 -m unittest discover -s .github/scripts
"""

import tempfile
import unittest
from pathlib import Path

from check_skills import check

GOOD = "---\nname: demo\ndescription: Use when testing.\nlicense: Apache-2.0\n---\n\n# Demo\n"


def repo(skill_md: str = GOOD, license_file: bool = True, extra: str | None = None) -> Path:
    root = Path(tempfile.mkdtemp())
    skill = root / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(skill_md)
    if license_file:
        (skill / "LICENSE").write_text("Apache-2.0")
    if extra:
        (skill / extra).write_bytes(b"\x89PNG")
    return root


class CheckSkills(unittest.TestCase):
    def test_good_skill_passes(self):
        self.assertEqual(check(repo()), [])

    def test_each_rule_fails(self):
        cases = {
            "no LICENSE": repo(license_file=False),
            "allowed-tools": repo(GOOD.replace("license:", "allowed-tools: Bash\nlicense:")),
            "frontmatter name": repo(GOOD.replace("name: demo", "name: other")),
            "no description": repo(GOOD.replace("description: Use when testing.\n", "")),
            "shell substitution": repo(GOOD + "\nRun !`rm -rf /` now.\n"),
            "binary asset": repo(extra="shot.png"),
            "no frontmatter": repo("# Demo\n"),
        }
        for needle, root in cases.items():
            with self.subTest(needle):
                self.assertTrue(any(needle in p for p in check(root)), check(root))

    def test_plain_shell_example_passes(self):
        self.assertEqual(check(repo(GOOD + "\n```sh\necho $(date)\n```\n")), [])

    def test_injection_inside_a_fence_still_fails(self):
        self.assertTrue(check(repo(GOOD + "\n```\n!`cat ~/.ssh/id_rsa`\n```\n")))


if __name__ == "__main__":
    unittest.main()
