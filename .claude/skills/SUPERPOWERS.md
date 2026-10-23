# Superpowers skills (vendored)

These skills are copied unchanged from [obra/superpowers](https://github.com/obra/superpowers)
at commit `2ce8aa15d05bd57213e6f746f953aafb7ded7a8b` (2026-10-21), MIT licence (see
`SUPERPOWERS-LICENSE`). They are vendored as project skills so that every Claude Code session
in this repository, local or cloud, has them without installing the plugin.

- References of the form `superpowers:<name>` inside the skills mean `.claude/skills/<name>`.
- The plugin's SessionStart hook is not vendored; `CLAUDE.md` tells the agent to read
  `using-superpowers` at the start of every session instead.
- Do not install the superpowers plugin on top of this copy in the same project, or every
  skill is listed twice.
- To update: copy `skills/` from a newer upstream commit over this folder and change the
  commit above.
