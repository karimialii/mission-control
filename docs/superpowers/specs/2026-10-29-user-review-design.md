# User review of agent reports

## Goal
Let the user review executor reports themselves (approve, or request changes with written feedback) instead of the supervisor, chosen per task with a per-phase shortcut. The supervisor can suggest which reports deserve the user's eyes; the user accepts or hands the review back.

## Data model
Two optional fields on each task JSON (`.mission/tasks/T-xxx.json`):
- `reviewer`: `"supervisor"` (default, when absent) or `"user"`. AI tasks only; human (teammate) tasks keep their PR-based review.
- `review_suggest`: `{reason: str, state: "pending" | "accepted" | "declined", at: iso-time}`, set by the supervisor.

## Flow when `reviewer == "user"`
1. The executor finishes; the task goes to `review` as today.
2. `needs_decision()` (mc.py) ignores `report` events for tasks with `reviewer == "user"`, so the supervisor is not woken. The events still ride along in its next update.
3. The dashboard lists the task under **Needs your review** (Overview and Phases) and shows a badge on its card. CHECK FAILED / RUNNER ERROR / PERMISSION DENIED flags show as they do for the supervisor.
4. The task drawer shows **Approve** and **Request changes** (feedback box, required) when `status == "review"` and `reviewer == "user"`.
5. Both buttons call `POST /api/review {t, verdict, message}` which calls `review_task(m, tid, verdict, msg, by="user")`. Approve merges in git mode; Request changes writes `feedback/T-xxx.md`, sets `needs_changes`, and schedules a re-run if autopilot is on.
6. The supervisor is never asked to approve a user-reviewed task, and autopilot never auto-approves one. The supervisor's inbox line for such a task reads `REVIEW T-xxx … (reviewer: user)` so it does not act on it.

## Choosing the reviewer
- `POST /api/reviewer {t | phase, reviewer}` sets the field for one task, or for every non-final AI task of a phase. A per-task change afterwards overrides the phase one.
- The Phases view shows an **I'll review this phase** checkbox on each phase header, and the drawer/card has a per-task You / Supervisor toggle. The plan-approval panel shows the same controls so the choice can be made before launch.
- CLI: `mc reviewer T-xxx user|supervisor` (and `--phase N`).
- Switching to `supervisor` while the task is already in `review` wakes the supervisor.

## Supervisor suggestions
- **Plan time:** a task in the plan JSON may carry `"review_suggest": "<why>"`. The approval panel lists these ("Supervisor suggests you review T-004: <reason>") with **Accept** (reviewer becomes `user`, state `accepted`) and **Keep with supervisor** (state `declined`).
- **Report time:** `mc suggest-review T-xxx "<reason>"` sets `review_suggest` (pending) on a task in `review` instead of approving it. The dashboard shows a card in the review queue: **I'll review it** or **You decide**. Declining wakes the supervisor to decide. An unanswered suggestion leaves the task in `review` without blocking other work, and nothing auto-approves it.
- The supervisor prompt (SUPERVISOR rules in mc.py) gains a short rule: suggest sparingly, only for risk, low confidence, failed checks or irreversible changes, and always with a reason.

## Edge cases
- Final tasks (`done`, `cancelled`) ignore reviewer changes.
- `review_task` is unchanged apart from recording `by="user"` in events (it already takes `by`).
- Old tasks without the fields behave as `reviewer == "supervisor"`.

## Files touched
- `scripts/mc.py`: `needs_decision`, inbox line, `set_reviewer`, `suggest_review`, `mc reviewer` / `mc suggest-review` commands, plan parsing for `review_suggest`, supervisor prompt rule.
- `scripts/dashboard.py`: `/api/review`, `/api/reviewer`, `/api/review-suggest` (accept/decline), state payload includes the new fields.
- `dashboard/index.html`: queue, drawer buttons, phase checkbox, approval-panel suggestions.
- `README.md` / `SKILL.md`: document the feature.

## Testing
No test suite exists in the repo, so add a small `scripts/test_user_review.py` (stdlib `unittest`) covering: `needs_decision` skipping user-reviewed tasks, `review_task(by="user")` for both verdicts, phase-level reviewer setting with per-task override, and the suggest-review state transitions. Then verify the UI in the browser pane against a throwaway project.
