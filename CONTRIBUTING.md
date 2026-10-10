# Contributing to honed-skills

Thank you for your interest. The rules for what a skill must be are in the
[README, under Contributing](README.md#contributing). This file is the procedure.

## Ways to help

- **Report a skill that misfires.** It loads when it should not, does not load when it should, or
  gives an instruction that fails on your machine. Use the **A skill misbehaves** issue form.
- **Propose a skill.** Tell us the task, and what went wrong the first time you did it for real.
  A skill here is extracted from real work, so the gotchas matter more than the happy path. Use
  the **Propose a skill** issue form before you write one.
- **Improve a skill.** A new gotcha, a check that should fail and does not, a platform it breaks
  on. Small pull requests are welcome.

## Before you open a pull request

1. Run the repository check: `python3 .github/scripts/check_skills.py`. It checks the frontmatter,
   the `LICENSE` in every skill folder, no `allowed-tools`, no `` !`command` `` injection, and no
   binary asset inside a skill folder.
2. Run the skill's own selftest, if it has one, and quote its output in the pull request.
3. Scan the skill with [NVIDIA SkillSpector](https://github.com/NVIDIA/SkillSpector) and read the
   JSON report. Fix each finding, or explain it in a `.skillspector-baseline.yaml` as the README
   describes.
4. Use placeholder values only (`example.com`, invented names). No private data.
5. Write the pull request title as a [Conventional Commit](https://www.conventionalcommits.org/),
   for example `feat(qr-code): read a code from a photo`.

## Contributor License Agreement

Before we can merge your first pull request, you sign the
[Plexito Contributor License Agreement](https://github.com/Plexito-de/.github/blob/main/cla/INDIVIDUAL.md).
A bot asks you on the pull request, and you sign with one comment. It is a license, not a
transfer: you keep the copyright in your work. If you contribute for your employer, your employer
signs the [Entity Agreement](https://github.com/Plexito-de/.github/blob/main/cla/ENTITY.md) first.

## Code of Conduct

This project follows the
[Plexito Code of Conduct](https://github.com/Plexito-de/.github/blob/main/CODE_OF_CONDUCT.md).
Report unacceptable behavior to [info@plexito.de](mailto:info@plexito.de).
