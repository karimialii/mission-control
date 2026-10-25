---
description: Show where this project's mission stands: tasks, what needs you, cost
allowed-tools: Bash(python3 *mission-control/scripts/mc.py status*), Bash(python3 *mission-control/scripts/mc.py inbox*)
---
Show me where this project's Mission Control mission stands. Run these two commands with the Bash tool, exactly as written, and base your answer only on their output:

1. `python3 ~/.claude/skills/mission-control/scripts/mc.py status --soft`
2. `python3 ~/.claude/skills/mission-control/scripts/mc.py inbox --peek --soft`

Summarise in under six lines: what is finished, what is running, and anything that needs me. If the output says there is no mission in this folder, say so and mention `/mission-control-start`.
