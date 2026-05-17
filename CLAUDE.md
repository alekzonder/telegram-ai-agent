# Claude Instructions

Follow `AGENTS.md`. This is a public generic Telegram bot runtime, not a
personal assistant repository.

Before editing, inspect the relevant files and keep changes scoped. Do not copy
private prompts, private project knowledge, local runtime configs, secrets, or
machine-specific deployment files into this repository.

Use the public project knowledge skill when you need architecture or release
context:

- `.claude/skills/project-knowledge/SKILL.md`

Use `.claude/skills/bot-setup/SKILL.md` for installation, systemd autostart,
language, commands, and operator troubleshooting. Use
`.claude/skills/topic-setup/SKILL.md` for Telegram forum topic wiring.

Run the standard checks before final handoff:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src/ mcp-servers/bot/server.py
uv run pytest
```


<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:ca08a54f -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

## Session Completion

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   bd dolt push
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** - Clear stashes, prune remote branches
6. **Verify** - All changes committed AND pushed
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds
<!-- END BEADS INTEGRATION -->
