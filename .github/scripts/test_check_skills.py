"""Purpose: prove check_skills.py fails on each rule it claims to check.
Usage:   python3 -m unittest discover -s .github/scripts
"""

import tempfile
import unittest
from pathlib import Path

from check_skills import check, warn

GOOD = "---\nname: demo\ndescription: Use when testing.\nlicense: Apache-2.0\n---\n\n# Demo\n"


def repo(
    skill_md: str = GOOD,
    license_file: bool = True,
    extra: str | None = None,
    template_options: list[str] | None = None,
    template: bool = True,
) -> Path:
    root = Path(tempfile.mkdtemp())
    skill = root / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(skill_md)
    if license_file:
        (skill / "LICENSE").write_text("Apache-2.0")
    if extra:
        (skill / extra).write_bytes(b"\x89PNG")
    if template:
        options = ["demo"] if template_options is None else template_options
        tmpl = root / ".github" / "ISSUE_TEMPLATE" / "misbehaves.yml"
        tmpl.parent.mkdir(parents=True)
        tmpl.write_text(
            f"name: A skill misbehaves\nbody:\n  - type: dropdown\n    id: skill\n    attributes:\n      options: [{', '.join(options)}]\n"
        )
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
            "no misbehaves.yml template": repo(template=False),
            "options do not match skills/": repo(template_options=["other"]),
        }
        for needle, root in cases.items():
            with self.subTest(needle):
                self.assertTrue(any(needle in p for p in check(root)), check(root))

    def test_plain_shell_example_passes(self):
        self.assertEqual(check(repo(GOOD + "\n```sh\necho $(date)\n```\n")), [])

    def test_injection_inside_a_fence_still_fails(self):
        self.assertTrue(check(repo(GOOD + "\n```\n!`cat ~/.ssh/id_rsa`\n```\n")))

    def test_description_over_the_spec_limit_fails_in_both_yaml_forms(self):
        long = "word " * 220  # 1,100 characters
        plain = GOOD.replace("Use when testing.", long.strip())
        folded = GOOD.replace("description: Use when testing.", "description: >-\n  " + long.strip())
        continued = GOOD.replace("description: Use when testing.", "description: Use when\n  " + long.strip())
        for name, md in (("plain", plain), ("folded", folded), ("plain, continued", continued)):
            with self.subTest(name):
                self.assertTrue(any("1,024" in p for p in check(repo(md))), check(repo(md)))

    def test_folded_description_under_the_limit_passes(self):
        folded = GOOD.replace("description: Use when testing.", "description: >-\n  Use when\n  testing.")
        self.assertEqual(check(repo(folded)), [])

    def test_a_long_skill_md_warns_and_does_not_fail(self):
        root = repo(GOOD + "line\n" * 500)
        self.assertEqual(check(root), [])
        self.assertTrue(any("500 lines" in w for w in warn(root)), warn(root))
        self.assertEqual(warn(repo()), [])

    def test_block_template_options_passes(self):
        root = repo(template=False)
        tmpl = root / ".github" / "ISSUE_TEMPLATE" / "misbehaves.yml"
        tmpl.parent.mkdir(parents=True)
        tmpl.write_text(
            "name: A skill misbehaves\nbody:\n  - type: dropdown\n    id: skill\n    attributes:\n      options:\n        - demo\n"
        )
        self.assertEqual(check(root), [])

    def test_misbehaves_options_mismatch_reports_missing_and_extra(self):
        root = repo(template_options=["other", "extra"])
        problems = check(root)
        self.assertTrue(any("missing: demo" in p and "extra: extra, other" in p for p in problems), problems)


if __name__ == "__main__":
    unittest.main()
