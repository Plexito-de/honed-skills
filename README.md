# honed-skills

[Agent Skills](https://agentskills.io/specification), honed against real work rather than written from imagination. Each skill here was extracted after doing the task for real, and it keeps the parts that only show up once you have hit them: the failure that looked like success, the flag that lied, the check that has to happen before you can claim the job is done.

The Agent Skills format is an open, cross-agent spec, so nothing here is tied to one product. A skill is a folder with a `SKILL.md`; the agent reads the `description`, decides the task matches, and loads the rest. Everything below is plain Python and command-line tools.

## Skills

| Skill | Use it when |
| --- | --- |
| [`comb-pdf-form-filling`](skills/comb-pdf-form-filling/) | An official PDF form has to be filled so that each character lands inside its own printed box (a comb field): IBAN, tax number, BIC, dates. Built against German Behörden, tax and bank forms, and applies to any AcroForm or flat PDF with printed boxes. |

## Install

Copy the skill folder into your agent's skills directory. Name the destination explicitly:

```bash
git clone https://github.com/Plexito-de/honed-skills
mkdir -p ~/.claude/skills
cp -r honed-skills/skills/comb-pdf-form-filling ~/.claude/skills/comb-pdf-form-filling
```

Name the destination as above rather than ending the path at the parent. `cp -r src dest/` on a machine where `dest` does not exist yet copies the *contents* instead of the folder, so the skill lands as `~/.claude/skills/SKILL.md`, exits 0, prints nothing, and can never load. Verify with `ls ~/.claude/skills/comb-pdf-form-filling/SKILL.md`.

**Where the directory is** depends on your agent, and several agents share one:

| Agent | Personal skills directory |
| --- | --- |
| Claude Code, Claude | `~/.claude/skills/` |
| Codex, GitHub Copilot CLI, Gemini CLI | `~/.agents/skills/` (shared cross-runtime path; Gemini CLI also reads `~/.gemini/skills/`, and `~/.agents/skills/` wins when both exist) |
| Others | Check your agent's own docs. The [client showcase](https://agentskills.io/clients.md) lists the products that support the format, including Cursor, VS Code, OpenCode, OpenHands, Goose, Amp, Junie, Factory, Roo Code and Kiro. |

Most agents also read a project-level directory (commonly `.claude/skills/` or `.agents/skills/`) if you would rather commit a skill alongside a repo than install it per machine.

To update, `git pull` and copy again: a copied folder is a snapshot and does not track this repo. To uninstall, delete the folder you copied.

## What "honed" means here

A skill in this repo has to earn its lines:

- **Written after the work, not before.** The method is what actually produced a correct artifact, including the attempts that did not.
- **Verification is part of the method.** If a step can silently produce a wrong-looking-right result, the skill says how to look at the output and what to compare it against. In `comb-pdf-form-filling` that means rendering the finished PDF to an image and reading it, because a filled form can look correct in one viewer and broken in another, and a checkbox written to the wrong state reports success while granting nothing.
- **The gotchas are the payload.** A table of symptoms and causes beats a tidy description of the happy path, and it is the part you cannot get from an API reference.
- **Prior art is researched first and credited.** Where a public skill already covers ground, we read it at the source, take the ideas that hold up, say so, and state what ours adds. We do not copy code or text: a borrowed method you have not tested is worse than no method, and an unread dependency is a supply-chain risk.
- **No private data.** Examples use placeholder values only.

## Contributing

Issues and pull requests are welcome. A new skill needs a `SKILL.md` with `name` and `description` frontmatter, a gotchas section drawn from real use, and placeholder-only examples.

Two rules exist because a skills repo does not distribute documentation, it distributes instructions that land in someone else's agent context along with code that agent may run:

- **No `allowed-tools` declaration and no shell substitution in a skill body.** A skill must not pre-approve tools for itself or smuggle command execution into its text. If a skill needs a command run, it says so in prose and the human decides.
- **Scripts stay readable and dependency-light.** Anything under `scripts/` should be short enough to audit in one sitting, and its dependencies named in the frontmatter `compatibility` field.

## License

[Apache-2.0](LICENSE). Each skill folder carries a copy, so the licence travels with it when you copy the folder out.
