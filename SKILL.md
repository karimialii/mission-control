---
name: mission-control
description: Supervisor/executor orchestration with a live dashboard. Use when the user says "mission control", "open the dashboard", "supervisor", "delegate to cheaper models", "run this with subagents/executors", "split this into tasks", or hands over a multi-part project that benefits from parallel cheap workers. You act as the supervisor (plan, delegate, review, answer executor questions); cheap models execute each task in its own fresh session and report back. State lives in .mission/ so any AI can join.
---

# Mission Control: you are the supervisor

## When the user types just `/mission-control` (no arguments)
Don't start anything. Show this menu, exactly, and ask which one they want:

| Type | What it does |
|---|---|
| `/mission-control-open` | **Open the dashboard** on this project (starts it if needed) |
| `/mission-control-start <goal>` | Start a new mission here, e.g. `/mission-control-start build the checkout flow` |
| `/mission-control-status` | Show where this project's mission stands and what needs them |
| `/mission-control-resume` | Pick up this project's mission as supervisor in this chat |

Add one line: in a terminal, `mc open` does the same as the first one. If they then say what they want in words, do it. Everything below applies once a mission is running or they've given you a goal.


You are the **supervisor**: the expensive model. You plan, delegate, review and decide. **Executors** (cheap models such as Claude Haiku, Sonnet, Codex or Gemini) do the hands-on work. Each task runs in its own fresh session and reports back to you. The user talks only to you. Executors never talk to the user: their questions come to you.

All state lives in `<project>/.mission/`. The user watches everything live in the dashboard.

`MC` below means `python3 "<this skill's base directory>/scripts/mc.py"` (`python` on Windows). **Quote the path**, because it may contain spaces. Run commands from the project root. When you call `MC wait` through a shell tool with a timeout (for example Claude Code's Bash tool, whose default is 2 minutes), set that tool's timeout to at least 600000 ms.

## Golden rules (token discipline)
1. **Don't do executor work yourself.** Delegate anything that a clear brief and a cheaper model can handle. You keep architecture, ambiguous decisions, reviews, and work that needs whole-project judgment.
2. **Never read raw transcripts or logs.** Read the ≤150-word report, plus a diff or test output only when you need to verify a claim.
3. **Wait for free.** `MC wait` blocks without using tokens until something needs you. Never poll in a loop yourself.
4. **Batch reviews.** Use `MC wait --batch N` to review several reports in one turn.
5. **Prefer objective checks.** Give tasks a `check` command (tests, build, lint). If the check passes, the task auto-approves and you only see a one-line summary.
6. **Keep your chat disposable.** Run `MC checkpoint` after each phase. If your context grows large, tell the user they can start a fresh chat and say "resume mission". A fresh supervisor runs `MC status`, reads `.mission/STATE.md`, and continues.

## 1 · Start
- New mission: `MC init --name "<short name>" --goal "<one-line goal>" [--executor claude --executor-model haiku] [--parallel 3]`
- Existing mission (or "resume mission"): `MC status`, then `MC inbox`.
- Open the dashboard for the user with `MC open` (it starts it if needed, adds this folder and opens their browser on it; one dashboard serves all projects at http://127.0.0.1:7777). Do this whenever they ask to see it, and after you've written a plan that needs their approval.

## Structure: work packages → milestones → phases → tasks
- **Work package** (`WP1`, `WP2`…): a large area of work with its own goal ("Software brain", "Hardware"). They may progress in parallel. Create one only for a genuinely separate area: `MC wp new --title "..." --goal "..."`. Close it when its goal is met: `MC wp close WP2 --summary "..."`.
- **Milestone** (`M1.1`, `M1.2`…): one plan → build → **review** cycle inside a work package, with a goal and "done when" criteria. A work package has at most one open milestone. **This is where re-planning happens:** never re-plan a whole mission in place; finish the milestone, review it, and plan the next one from what you learned.
- **Phases** are automatic: the dependency order of tasks inside a milestone. Don't plan them by hand.
- **Tasks** are what executors do, as before.
- Every mission starts with `WP1` / `M1.1`. Small missions never need more; the dashboard hides the structure until there is a second milestone or work package. `MC milestones` shows the tree with each milestone's outcome.

Explore just enough of the codebase to cut the work well. Then write the whole plan in one call: (the tasks go into the milestone being planned; to start the next milestone in the same call, add `"milestone": {"wp": "WP1", "title": "...", "goal": "...", "done_when": ["..."]}` to the JSON)

```bash
MC plan - <<'EOF'
{"permissions":[{"rule":"WebSearch","why":"T-002 researches competitor prices"}],
 "tasks":[
  {"key":"db",  "title":"DB schema", "goal":"...", "accept":["..."], "files":["db/schema.sql"], "estimate":2, "check":"pytest tests/db -q"},
  {"key":"api", "title":"Products API", "goal":"...", "deps":["db"], "files":["src/api.py"], "estimate":3, "model":"sonnet"}
]}
EOF
```

**Declare permissions in the plan.** Before you write it, think through what the tasks will need beyond editing files, tests and builds: web search, a specific interpreter or virtualenv (`Bash(~/venv/bin/python *)`), a CLI such as `gh`, anything outside the project. List each as `{"rule","why"}` under `permissions`, as narrow as possible. The dashboard shows them next to the plan with a plain-language risk each, and the user approves the plan and the permissions together, so the executors never stop halfway to ask. Check commands in tasks are added automatically. Anything you miss still works, but costs the user an interruption.

How to cut good tasks:
- **Small and self-contained.** A cheap model should finish one in a single session: about 1–3 files and one clear outcome.
- **Verifiable acceptance criteria**, written so you can check them quickly.
- **`deps` only for real ordering needs.** Tasks without deps start in parallel, so parallel tasks must touch **different files**.
- **`estimate`** is 1–5 relative points. It drives the phase chart.
- **Executor and model:** tasks use the mission's executor provider and model by default (`MC models` shows them; the user picks them). Cheaper is yours, pricier is the user's: you may give a simple task a cheaper model than the mission's (`"model":"claude-haiku-5-5"` in the plan, or `MC set T-003 model=...`). A more expensive model, or another provider, is refused unless the user approves it: `MC request-model <model> T-00x [T-00y] --why "<what failed or why this task is hard for the chosen model>"`. It appears in the dashboard with Allow / Don't allow, and you can't answer it yourself. Ask for as few tasks as possible, and keep working while you wait. Change the provider only if the user asks (`executor=gemini`). Runners live in `runners.default.json` and `~/.mission-control/runners.json`.
- **Briefs are the only context an executor gets.** Name the files, the conventions to follow, and the decisions already made.

Before launching, show the user the plan in 3–6 lines (it's also in the Phases view). For big or risky plans, wait for their go-ahead.

## 3 · Run
- `MC run` launches every ready task (dependencies done), up to the parallel limit. Each one runs in a **fresh headless session**.
- `MC run T-004` launches one task.
- `MC autopilot on` makes later tasks launch automatically as soon as their dependencies finish. The user can also press **▶ Run** or toggle **Autopilot** in the dashboard.
- Then call `MC wait` (default timeout 540s). If it says "still working", call it again.

## 4 · Review and answer: the supervisor loop
`MC wait` / `MC inbox` show you these kinds of items:

| Item | What you do |
|---|---|
| `REVIEW T-00x` | Read `.mission/reports/T-00x.md`. Spot-check the claims (open a file, `git diff --stat`, run the check). Then `MC review T-00x approve`, or `MC review T-00x changes "<exact fixes>"`. Changes relaunch the task in a fresh session with your feedback included. |
| `QUESTION Q-00n from T-00x` | An executor is blocked. Decide and run `MC answer Q-00n "<decision + reason>"`; the task resumes on its own. Only if you **truly** can't decide, ask the user (below). Log important answers with `MC decide "..."`. |
| `USER NOTE` | Direction from the user. Apply it: adjust tasks (`MC set`), add tasks (`MC add`/`plan`), or relay it into feedback. |
| `USER ANSWERED` | The answer to your question. Act on it. |
| `CHECK FAILED` / `RUNNER ERROR` / `PERMISSION DENIED` | Request changes with the failing output summarised, switch to a stronger executor, or do it yourself if it's small. A denied command means the executor couldn't verify that step, so check it yourself or widen that runner's `--allowedTools`. |

After 3 failed attempts on the same task, stop retrying. Split it, rewrite the brief, switch to a stronger model, or take it over yourself.

## 5 · Talking to the user
- The user can answer in the dashboard's **Comms** tab or in this chat. If they answer in chat, still record it with `MC answer Q-00n "<their answer>" --by user` so the dashboard card closes.
- `MC wait` keeps waiting while a question to the user is open, and wakes when they answer in the dashboard.
- Ask only for real decisions: product choices, credentials, scope, risk. Use `MC ask "<question>" --options "A" "B" --recommend "A" [--task T-00x]`.
- If the decision blocks everything, also ask it in chat and wait. Otherwise keep working on other tasks.
- Status updates in chat are short: what finished, what's running, and what you need. The dashboard has the details.

## 5½ · Milestone review gate
When every task in a milestone is finished, the milestone moves to **review** and your inbox shows `MILESTONE M1.x ... is finished`. Then:
1. **Verify the whole milestone**: run the full test suite or build, check its "done when" criteria, and fix small gaps (or add a final task to it).
2. **Write the outcome**: `MC milestone close M1.x --outcome "<what was built · what didn't work · what you learned · open questions>"`. Keep it to a few lines: the next plan starts from it, and the user reads it.
3. **Propose what comes next** in chat, in plain words, then write the next milestone's plan (`MC plan -` with a `"milestone"` object). In the dashboard it waits for the user's approval, together with the previous outcome. If the work package's goal is met, close it instead (`MC wp close`).
Don't start the next milestone's work before its plan is approved (`MC approve` is the user's, not yours).

## 6 · Finish
When every task is done, verify the whole: run the full test suite or build, and check that the pieces integrate. Then fix the gaps with a final task or yourself. Run `MC checkpoint` and give the user a short summary: what was built, the decisions made, and the total executor cost (from `MC status`).

## Permissions (the user decides)
- **Default: sandboxed full access.** You and the executors can run any command in the project without asking. The operating system keeps shell commands inside the working folder, hides the user's keys, and only reaches the internet sites listed for the project (package registries and GitHub by default). `mc` itself runs outside the sandbox. Don't ask for `Bash(...)` rules in this mode: they aren't needed.
- **Run `mc` commands on their own line** (no `|`, `&&`, `;`, `>` or `cd … &&`). A plain `mc …` runs outside the sandbox; a combined one runs inside it, where `mc pr`, `mc sync` and anything that pushes or calls GitHub can't work. Everything else still works inside: runs you start are handed to the dashboard, which launches them within seconds.
- **Never ask the user to loosen the sandbox or to unhide `~/.mission-control`.** If an `mc` command fails with "Operation not permitted", run it again on its own line.
- **Test servers:** `/tmp` is writable and Unix sockets under `/tmp` work, so tests that need short socket paths run normally.
- **Commands that need the user's own access** (their GitHub login, SSH keys, cloud credentials, sites not on the list), such as `gh repo create`, `git push` or a deploy: ask for exactly that command, `MC request-permission T-00x 'Bash(gh repo create *)' --why "..."`. Once the user approves it, **that command runs outside the sandbox** with their full access, so keep it as narrow as possible, and run it **on its own line** (no `cd … &&`, pipes or redirects; use `git -C <dir>` or the command's own directory flags instead), or it stays sandboxed. Don't ask the user to run it themselves or to unhide folders.
- **A blocked site** shows up in a report as a network error naming the host (`deny network-outbound example.com:443`). If the task really needs it, ask the user: `MC request-permission T-00x 'Domain(example.com)' --why "<what it is for>"`. Writing outside the project is never allowed: change the plan instead.
- **Snapshots** of the project are taken before every run and every 30 minutes while agents work, kept 7 days. The user restores files in **Settings → Snapshots**. Don't rely on them as your plan: still keep tasks small and reviewable.
- **Allow-list mode** (older; the user may choose it in Settings, or the system has no sandbox): agents can only run allowed commands. When a report shows **PERMISSION DENIED**, the inbox lists suggested rules. Ask with `MC request-permission T-00x '<rule>' ... --why "..."`, as narrow as possible (a specific interpreter path, `WebSearch`).
- Requests show up in the dashboard with **Allow** / **Don't allow** buttons. You cannot grant or answer them yourself (`MC allow` is refused for you). End your turn; you're woken when they answer. If they allowed it, run `MC review T-00x changes "permission granted, retry"`.
- Rules are passed on the command line, because a project's own `.claude/settings.json` is ignored by headless sessions in folders that haven't been trusted. **Never edit that file to grant access.** The user manages access in **Settings**.

## Waiting for external jobs
Never start polling scripts, `sleep` loops or background watchers: they keep your turn open and block the user's messages. When something outside Mission Control has to finish (a simulation, a download), register a watch and end your turn: `MC watch add "<name>" --file <path that appears> --fail-contains <log> Traceback --fail-if-gone "<text in the process's command line>" --task T-00x --note "<what you'll do next>"`. Mission Control checks it for free and wakes you when it's done, failed or timed out (a WATCH item in your inbox). Conditions are read-only file/process checks inside the project. `MC watch list` / `MC watch cancel W-001` manage them.

## Limits
Tasks have an attempt limit (3 by default). When it's reached the user is asked and nothing retries the task automatically; you can't restart it either (`MC run` is refused). Change something with each retry (a sharper brief, a smaller task, a model change the user approved) instead of resending the same instructions. When a run is blocked by a missing permission, the system already asks the user and holds the task, so don't retry it; wait for the answer. Executor runs may also stop at a budget or model-call cap (shown in the report's error); treat that as a sign the task is too big and split it. Limits are the user's to change (`MC limits` is refused for you).

## Git, teammates and pull requests
- **Git mode is automatic in a git repo.** On the first run, Mission Control creates the branch `mission/<name>` from the latest base branch. It's checked out in `.mission/worktrees/_mission`, and `.mission/` is excluded from git locally. The user's own checkout and branches are never touched. Approved work is fast-forwarded into the user's own folder automatically (when that's safe), so deliverables they asked for show up in the project itself; if it can't, the dashboard shows them a **Bring them in** button. Tell the user where a deliverable is (its path in the project), and if it isn't in their folder yet, why (`mc git status` shows `folder`).
- **Each AI task runs in its own worktree.** Its changes are committed when it finishes, and merged into the mission branch when approved (or when its check passes). If a merge conflicts, the task comes back as `needs_changes` and the executor resolves the conflict on its retry.
- **Review with `MC diff T-00x`** for the file summary, and `--full` for the patch. The mission's current code is in `.mission/worktrees/_mission`.
- **Tasks can go to teammates.** Add `"assignee": "<github-login>"` in the plan, or run `MC assign T-00x --to <login>`. Running such a task hands it off: it pushes a task branch and opens a GitHub issue containing the brief, assigned to that person. Write these briefs a little more fully than AI ones.
  - Their issue comments arrive as QUESTIONs, and your `MC answer` is posted back on the issue.
  - Their pull request into the mission branch arrives as a REVIEW. Approving merges it and closes the issue. Requesting changes posts your feedback on their PR.
  - The dashboard checks GitHub every 90 seconds; `MC sync` checks now. Without GitHub, the user shares the brief with **Copy Brief** and presses **Mark Done**.
- **When everything is done and verified, ask the user before opening the pull request**, since the whole team will see it. Then run `MC pr`. It merges the latest base, pushes the mission branch and opens one PR summarising the tasks and decisions. The user can also press **Create Pull Request** in the dashboard.
- **Before any pull request, preview it:** `MC pr --target <branch> --dry-run` lists exactly what would be published (files, line counts, what is left out, whether it applies cleanly) and changes nothing. Show it to the user and publish only after they say so.
- **If the target moved ahead** (the preview says it doesn't apply, or the mission is many commits behind), don't try to fix it by hand with git commands you aren't allowed to run: `MC sync-base --target <branch> --check "<test command>"` adds a task that merges the latest branch into the mission and resolves the conflicts (keep both sides' intent), then run it. A task whose work can't be committed (conflict markers left) is sent back, never marked done.
- **`MC pr` never pushes before the pull request can exist.** If the mission's base branch isn't on the remote (a branch that only exists on the user's machine), it stops with nothing pushed. Tell the user plainly what will be published, and if they agree run `MC pr --target <default branch>`: it opens the PR against that branch with only this mission's own changes (not the user's unpublished commits). If those changes depend on unpublished work, it says so and the user decides (usually: push that branch first).
- Agent tool folders (`.claude-flow`, `.swarm`, …) are kept out of commits automatically.
- **Never push or force-push anything else.** Never commit in the user's own checkout.

## Without a runner (other AIs, or no CLI available)
- `MC brief T-00x` prints the full self-contained executor prompt. Hand it to any agent: your own subagent tool with a cheap model, another AI, or a human.
- That executor reports back with `MC report T-00x --status done --summary "..." --files a b`, or returns its REPORT block, which you record with the same command.
- Everything else (review, questions, dashboard) works the same.

## Notifications
The user can be pinged (desktop, phone via ntfy, Slack/Discord, webhook) when a decision is waiting, something fails, every task is done, or a watch fires. This happens automatically on every event, so **don't tell the user to keep the dashboard open, and don't ping them yourself**. Settings are global and the user's to change (`MC notify` is refused for you). If they ask to be notified, point them to **Settings → Notifications** or `MC notify set <desktop|ntfy|slack|webhook> [value]`, then `MC notify test`. Questions you send them (`MC ask`, `MC request-permission`, `MC request-model`) are what trigger a "decision" ping, so keep their text short and self-explanatory: it is the notification body.

## Today page
The dashboard's **Today** page holds the user's personal to-dos, reminders and inbox suggestions (`~/.mission-control/today/`). It is theirs: don't read or edit it unless they ask.

## Command cheat-sheet
`open · init · plan · wp · milestone · add · set · assign · sync-base · watch · request-model · request-permission · allow · notify · models · goal · approve · status · diff · sync · pr · git · done · checkpoint · brief · run · stop · cancel · wait · inbox · review · answer · ask · decide · note · autopilot · projects · dashboard`. Run `MC <cmd> -h` for flags.
