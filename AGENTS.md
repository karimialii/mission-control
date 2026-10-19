# Mission Control (for any AI agent)

This folder is a portable supervisor/executor toolkit. It works with Claude Code, Codex, Gemini CLI, Cursor, or any agent that can run shell commands.

- **If you are the supervisor** (the user asked you to plan or delegate, or to "use mission control"): follow `SKILL.md`.
- **If you were given a task brief** (it starts with "Executor protocol"): follow `EXECUTOR.md`, and do only that task.

The CLI is `python3 scripts/mc.py` (`python scripts\mc.py` on Windows; Python 3.9+, stdlib only). All state lives in `<project>/.mission/`.
