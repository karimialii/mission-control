# User Review of Agent Reports Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user review executor reports themselves (approve / request changes with feedback), chosen per task with a per-phase shortcut, with supervisor suggestions the user can accept or hand back.

**Architecture:** Two optional task fields (`reviewer`, `review_suggest`) in `.mission/tasks/T-xxx.json`. `mc.py` gets the logic (helpers, wake rules, auto-approve rule, CLI); `dashboard.py` exposes three small POST endpoints that call those helpers; `index.html` adds the queue, drawer controls, phase checkboxes and plan-approval suggestions. The existing `review_task(..., by="user")` does the actual approve/changes work.

**Tech Stack:** Python 3 stdlib (`unittest`), single-file vanilla-JS dashboard. All paths below are relative to `/Users/ali/Documents/GitHub/Mission Control/mission-control`.

## Global Constraints

- Absent `reviewer` means `"supervisor"`; old tasks must behave exactly as before.
- Reviewer applies to AI tasks only; `is_human(t)` tasks keep their PR-based review.
- A task with `reviewer == "user"` must never be auto-approved (neither by the supervisor nor by a passing `check`), and must not wake the supervisor when it reaches `review`.
- Final tasks (`status` in `FINAL` = done/cancelled) ignore reviewer changes.
- Spec adjustment: the dashboard phase checkbox sends the list of task ids it shows (the UI computes phases per milestone), so `/api/reviewer` takes `{tasks: [ids], reviewer}`. The CLI `--phase N` uses `phase_levels`.
- Spec adjustment: the supervisor's inbox shows a user-reviewed task as an `FYI … Do NOT review it` line instead of a `REVIEW` line.
- Commit after each task. Commit message trailer: `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.

---

### Task 1: Core logic in mc.py (helpers, wake rule, auto-approve rule)

**Files:**
- Create: `scripts/test_user_review.py`
- Modify: `scripts/mc.py` (`new_task` ~line 361, `needs_decision` ~2006, check-pass block ~1600, initial `new_status` ~1572, inbox loop ~3297, new helpers after `review_task` ~2920)

**Interfaces:**
- Produces (all in `mc.py`):
  - `reviewer_of(t: dict) -> str` returns `"user"` or `"supervisor"`.
  - `set_reviewer(m, ids: list[str], reviewer: str, by: str = "user") -> list[str]` returns ids actually changed.
  - `suggest_review(m, tid: str, reason: str) -> None`.
  - `answer_suggestion(m, tid: str, accept: bool) -> None`.

- [ ] **Step 1: Write the failing tests**

Create `scripts/test_user_review.py`:

```python
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import mc  # noqa: E402


def make_mission():
    root = Path(tempfile.mkdtemp())
    m = mc.Mission(root)
    for d in ("tasks", "reports", "feedback", "questions", "runs"):
        (m.dir / d).mkdir(parents=True, exist_ok=True)
    mc.write_json(m.dir / "mission.json", {"name": "t", "phase": "executing", "max_attempts": 3})
    return m


def add_task(m, title="x", status="review", **kw):
    tid = m.next_id("T", "tasks")
    t = mc.new_task(m, {"title": title, **kw}, tid)
    t["status"] = status
    m.save_task(t)
    return tid


class ReviewerTests(unittest.TestCase):
    def setUp(self):
        self.m = make_mission()

    def test_default_is_supervisor(self):
        tid = add_task(self.m)
        self.assertEqual(mc.reviewer_of(self.m.task(tid)), "supervisor")

    def test_set_reviewer_user_and_back(self):
        tid = add_task(self.m)
        self.assertEqual(mc.set_reviewer(self.m, [tid], "user"), [tid])
        self.assertEqual(mc.reviewer_of(self.m.task(tid)), "user")
        mc.set_reviewer(self.m, [tid], "supervisor")
        self.assertEqual(mc.reviewer_of(self.m.task(tid)), "supervisor")

    def test_final_and_human_tasks_are_skipped(self):
        done = add_task(self.m, status="done")
        human = add_task(self.m, assignee="bob")
        self.assertEqual(mc.set_reviewer(self.m, [done, human], "user"), [])

    def test_bad_reviewer_rejected(self):
        tid = add_task(self.m)
        with self.assertRaises(ValueError):
            mc.set_reviewer(self.m, [tid], "nobody")

    def test_user_reviewed_report_does_not_wake_supervisor(self):
        tid = add_task(self.m)
        ev = [{"kind": "report", "task": tid, "actor": tid}]
        self.assertTrue(mc.needs_decision(self.m, ev))
        mc.set_reviewer(self.m, [tid], "user")
        self.assertFalse(mc.needs_decision(self.m, ev))

    def test_user_reviewed_blocked_still_wakes(self):
        tid = add_task(self.m, status="blocked")
        mc.set_reviewer(self.m, [tid], "user")
        self.assertTrue(mc.needs_decision(self.m, [{"kind": "report", "task": tid, "actor": tid}]))

    def test_user_review_approve_and_changes(self):
        a = add_task(self.m)
        b = add_task(self.m)
        mc.review_task(self.m, a, "approve", "", by="user")
        self.assertEqual(self.m.task(a)["status"], "done")
        mc.review_task(self.m, b, "changes", "rename it", by="user")
        self.assertEqual(self.m.task(b)["status"], "needs_changes")
        self.assertIn("rename it", self.m.feedback_text(b))

    def test_inbox_marks_user_reviewed_as_fyi(self):
        tid = add_task(self.m)
        mc.set_reviewer(self.m, [tid], "user")
        items = mc.inbox_items(self.m, mark_read=False)
        joined = "\n".join(items)
        self.assertIn("Do NOT review", joined)
        self.assertNotIn(f"REVIEW {tid}", joined)


class SuggestionTests(unittest.TestCase):
    def setUp(self):
        self.m = make_mission()

    def test_suggest_then_accept(self):
        tid = add_task(self.m)
        mc.suggest_review(self.m, tid, "touches auth")
        s = self.m.task(tid)["review_suggest"]
        self.assertEqual((s["reason"], s["state"]), ("touches auth", "pending"))
        mc.answer_suggestion(self.m, tid, True)
        t = self.m.task(tid)
        self.assertEqual(t["review_suggest"]["state"], "accepted")
        self.assertEqual(mc.reviewer_of(t), "user")

    def test_suggest_then_decline_keeps_supervisor(self):
        tid = add_task(self.m)
        mc.suggest_review(self.m, tid, "risky")
        mc.answer_suggestion(self.m, tid, False)
        t = self.m.task(tid)
        self.assertEqual(t["review_suggest"]["state"], "declined")
        self.assertEqual(mc.reviewer_of(t), "supervisor")

    def test_plan_spec_creates_pending_suggestion(self):
        tid = self.m.next_id("T", "tasks")
        t = mc.new_task(self.m, {"title": "p", "review_suggest": "big change"}, tid)
        self.assertEqual(t["review_suggest"]["state"], "pending")


if __name__ == "__main__":
    unittest.main()
```

Note: `mc.inbox_items` is the function that builds the `items` list shown near mc.py:3297 (the one containing the `for t in m.tasks(): if t["status"] == "review"` loop). If it has a different name, use `grep -n "def .*inbox\|items = \[\]" scripts/mc.py` to find it and adjust the test's call; keep `mark_read=False` semantics (the function already has a `mark_read` parameter).

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd scripts && python3 -m unittest test_user_review -v 2>&1 | tail -20`
Expected: errors/failures such as `AttributeError: module 'mc' has no attribute 'reviewer_of'`. If the fixture itself errors (for example a missing file the Mission needs), fix `make_mission()` until only the missing-feature errors remain.

- [ ] **Step 3: Implement helpers and rules in mc.py**

3a. In `new_task`, add to the returned dict (after `"check": ...`):

```python
        "reviewer": "supervisor",
        **({"review_suggest": {"reason": str(spec["review_suggest"]), "state": "pending", "at": now()}}
           if isinstance(spec.get("review_suggest"), str) and spec["review_suggest"].strip() else {}),
```

3b. Add right after `is_human` (~line 792):

```python
def reviewer_of(t: dict) -> str:
    """Who reviews this task's report: the user or the supervisor. Teammate (human) tasks keep their PR review."""
    return "user" if t.get("reviewer") == "user" and not is_human(t) else "supervisor"
```

3c. In `needs_decision`, replace the report clause:

```python
        if k == "report" and tasks.get(e.get("task"), {}).get("status") in ("review", "blocked"):
            return True
```

with:

```python
        if k == "report":
            t = tasks.get(e.get("task")) or {}
            if t.get("status") == "blocked" or (t.get("status") == "review" and reviewer_of(t) != "user"):
                return True
```

3d. In the report handler, change the initial line `new_status, step = "review", "awaiting supervisor review"` to:

```python
    new_status, step = "review", ("awaiting your review" if reviewer_of(t) == "user" else "awaiting supervisor review")
```

and change the auto-approve condition `if check_result["ok"] and not commit_error:` to:

```python
        if check_result["ok"] and not commit_error and reviewer_of(t) != "user":
```

(The check result is still written into the report; the task simply stays in `review`.)

3e. In the inbox loop (`for t in m.tasks(): if t["status"] == "review":`), add at the top of the loop body:

```python
            if reviewer_of(t) == "user":
                items.append(f"FYI {t['id']} ({t['title']}) is waiting for the USER's review (reviewer: user). Do NOT review it; they approve or request changes themselves.")
                continue
```

3f. Add after `review_task` (before `cmd_review`):

```python
def set_reviewer(m: Mission, ids: list, reviewer: str, by: str = "user") -> list:
    """Choose who reviews these tasks' reports. Final and teammate tasks are skipped. Returns the ids that changed."""
    if reviewer not in ("user", "supervisor"):
        raise ValueError("reviewer must be 'user' or 'supervisor'")
    changed = []
    for tid in ids:
        t = m.task(tid)
        if t["status"] in FINAL or is_human(t) or reviewer_of(t) == reviewer:
            continue
        m.update_task(tid, lambda x: x.update(reviewer=reviewer))
        m.event("task_updated", by, f"{tid} will be reviewed by {'you' if reviewer == 'user' else 'the supervisor'}", task=tid)
        changed.append(tid)
        if reviewer == "supervisor" and t["status"] == "review":
            wake_supervisor(m)
    return changed


def suggest_review(m: Mission, tid: str, reason: str) -> None:
    """Supervisor: ask the user to review this task's report themselves (they accept, or hand it back)."""
    t = m.task(tid)
    if t["status"] in FINAL or is_human(t):
        die(f"{tid} can't be suggested for user review ({t['status']})")
    m.update_task(tid, lambda x: x.update(review_suggest={"reason": reason, "state": "pending", "at": now()}))
    m.event("review_suggest", "supervisor", f"{tid}: supervisor suggests you review it: {short(reason, 140)}", task=tid)


def answer_suggestion(m: Mission, tid: str, accept: bool) -> None:
    """User: take the review (accept) or leave it to the supervisor (decline, which wakes it if the task is waiting)."""
    t = m.task(tid)
    sug = dict(t.get("review_suggest") or {})
    if not sug:
        return
    sug["state"] = "accepted" if accept else "declined"
    m.update_task(tid, lambda x: x.update(review_suggest=sug))
    if accept:
        set_reviewer(m, [tid], "user")
    elif t["status"] == "review":
        wake_supervisor(m)
    m.event("task_updated", "user", f"{tid}: {'you took' if accept else 'supervisor keeps'} the review", task=tid)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd scripts && python3 -m unittest test_user_review -v 2>&1 | tail -20`
Expected: all tests `ok`. Also run `python3 -m py_compile mc.py`.

- [ ] **Step 5: Commit**

```bash
git add scripts/mc.py scripts/test_user_review.py
git commit -m "feat: user-reviewed tasks (reviewer field, wake and auto-approve rules, suggestions)"
```

---

### Task 2: CLI commands and supervisor prompt

**Files:**
- Modify: `scripts/mc.py` (new `cmd_reviewer`, `cmd_suggest_review` near `cmd_review`; argparse near line 3832; prompt bullet near line 1930)
- Test: `scripts/test_user_review.py`

**Interfaces:**
- Consumes: `set_reviewer`, `suggest_review`, `phase_levels(tasks)` (already in mc.py; returns `{task_id: level}`).
- Produces: `mc reviewer <T-id> user|supervisor`, `mc reviewer --phase N user|supervisor`, `mc suggest-review <T-id> <reason...>`.

- [ ] **Step 1: Write the failing test**

Append to `ReviewerTests`:

```python
    def test_phase_selection_uses_dependency_levels(self):
        a = add_task(self.m, status="todo")
        b = add_task(self.m, status="todo", depends_on=[a])
        ids = mc.phase_task_ids(self.m, 1)
        self.assertEqual(ids, [b])
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd scripts && python3 -m unittest test_user_review.ReviewerTests.test_phase_selection_uses_dependency_levels`
Expected: `AttributeError ... phase_task_ids`

- [ ] **Step 3: Implement**

Add after `answer_suggestion`:

```python
def phase_task_ids(m: Mission, phase: int) -> list:
    """Ids of the tasks in dependency level `phase` (0-based), as the Timeline groups them."""
    tasks = m.tasks()
    levels = phase_levels(tasks)
    return [t["id"] for t in tasks if levels.get(t["id"]) == phase]


def cmd_reviewer(a):
    m = need_mission()
    if a.phase is not None:
        ids = phase_task_ids(m, a.phase - 1)
        who = a.first
    else:
        ids, who = [a.first], a.second
    if who not in ("user", "supervisor"):
        die("usage: mc reviewer <T-id> user|supervisor   or   mc reviewer --phase N user|supervisor")
    done = set_reviewer(m, ids, who, "supervisor")
    print(f"reviewer = {who} for: {', '.join(done) or 'nothing changed'}")


def cmd_suggest_review(a):
    m = need_mission()
    suggest_review(m, a.task, " ".join(a.reason))
    print(f"{a.task}: suggested to the user")
```

In the argparse block, after the `review` parser:

```python
    s = sub.add_parser("reviewer", help="choose who reviews a task's report: user or supervisor")
    s.add_argument("first"); s.add_argument("second", nargs="?"); s.add_argument("--phase", type=int)
    s.set_defaults(fn=cmd_reviewer)
    s = sub.add_parser("suggest-review", help="supervisor: suggest the user reviews this task's report themselves")
    s.add_argument("task"); s.add_argument("reason", nargs="+"); s.set_defaults(fn=cmd_suggest_review)
```

Note the `--phase` form: `mc reviewer --phase 2 user` parses `first="user"`. For the task form `mc reviewer T-001 user`, `first="T-001"`, `second="user"`.

Supervisor prompt: in the rules list, immediately before the line starting `- **Permissions belong to the user.**`, insert:

```
- **The user can review reports themselves.** Tasks may carry `"reviewer": "user"`; their reports show as `FYI … Do NOT review it` in your inbox, so leave them alone. Suggest user review sparingly, only for risk, low confidence, a failed check or an irreversible change, always with a reason: in the plan, add `"review_suggest": "<why>"` to a task; for a finished task, instead of approving it run `mc suggest-review T-00x "<why>"` and end your turn. The user either takes it or hands it back to you.
```

- [ ] **Step 4: Run tests and smoke the CLI**

Run: `cd scripts && python3 -m unittest test_user_review -v 2>&1 | tail -5 && python3 mc.py reviewer --help | head -3 && python3 mc.py suggest-review --help | head -3`
Expected: tests pass; both help texts print.

- [ ] **Step 5: Commit**

```bash
git add scripts/mc.py scripts/test_user_review.py
git commit -m "feat: mc reviewer and mc suggest-review commands, supervisor prompt rule"
```

---

### Task 3: Dashboard API endpoints

**Files:**
- Modify: `scripts/dashboard.py` (add three branches next to `/api/executor`, ~line 504)

**Interfaces:**
- Consumes: `mc.review_task`, `mc.set_reviewer`, `mc.answer_suggestion`, `mc.reviewer_of`.
- Produces: `POST /api/review {p, t, verdict: "approve"|"changes", message}`, `POST /api/reviewer {p, tasks: [ids], reviewer}`, `POST /api/review-suggest {p, t, accept: bool}`. The task JSON the dashboard already receives includes `reviewer` and `review_suggest`.

- [ ] **Step 1: Implement**

Insert before `elif u.path == "/api/executor":`:

```python
            elif u.path == "/api/review":
                t = m.task(tid)
                verdict, msg = body.get("verdict"), str(body.get("message", "")).strip()
                if verdict not in ("approve", "changes"):
                    return self._send(400, {"error": "verdict must be approve or changes"})
                if t["status"] != "review":
                    return self._send(409, {"error": f"{tid} isn't waiting for review ({t['status']})"})
                if verdict == "changes" and not msg:
                    return self._send(400, {"error": "write what should change"})
                out = mc.review_task(m, tid, verdict, msg, by="user")
                return self._send(200, {"ok": True, "result": out})
            elif u.path == "/api/reviewer":
                ids = [str(x) for x in (body.get("tasks") or []) if TASK_ID.fullmatch(str(x))]
                try:
                    changed = mc.set_reviewer(m, ids, str(body.get("reviewer", "")))
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
                return self._send(200, {"ok": True, "changed": changed})
            elif u.path == "/api/review-suggest":
                mc.answer_suggestion(m, tid, bool(body.get("accept")))
```

`/api/review-suggest` falls through to the generic OK response used by other branches (like `/api/resume`).

- [ ] **Step 2: Verify by hand against a throwaway server**

```bash
cd scripts && python3 -m py_compile dashboard.py && echo compiled
```

Expected: `compiled`. (Endpoint behavior is covered end to end in Task 5's browser check.)

- [ ] **Step 3: Commit**

```bash
git add scripts/dashboard.py
git commit -m "feat: dashboard endpoints for user review, reviewer choice and suggestions"
```

---

### Task 4: Dashboard UI

**Files:**
- Modify: `dashboard/index.html`:
  - `approveBanner()` (~line 1076): suggestions list.
  - `viewPhases()` (~line 924): "I'll review" checkbox on each phase chip, queue banner.
  - `viewBoard()` (~line 940): reviewer badge on cards, queue banner.
  - `renderDrawer()` (~line 1590): reviewer select + review box.
  - click delegation (~line 1626, handlers ~1644) and change handler (~line 1784).

**Interfaces:**
- Consumes: the three endpoints from Task 3; `post(path, body)` (adds project `p` itself; verify at index.html:674 and follow how neighboring calls pass `p`); `toast`, `tick`, `esc`, `md`.

- [ ] **Step 1: Add helpers (above `approveBanner`)**

```js
const aiTask=t=>t.executor!=="human";
const isUserRev=t=>aiTask(t)&&t.reviewer==="user";
function reviewQueue(){
  const st=S.st; if(!st) return "";
  const mine=st.tasks.filter(t=>t.status==="review"&&isUserRev(t));
  const asks=st.tasks.filter(t=>t.review_suggest?.state==="pending"&&t.status==="review"&&!isUserRev(t));
  if(!mine.length&&!asks.length) return "";
  return `<div class="panel approve"><div><div class="label" style="color:var(--purple)">Needs your review</div>
    ${mine.map(t=>`<div style="margin-top:6px"><span class="tag" data-task="${t.id}" style="cursor:pointer">${t.id}</span> ${esc(t.title)} <button class="btn sm primary" data-task="${t.id}">Review</button></div>`).join("")}
    ${asks.map(t=>`<div style="margin-top:6px"><span class="tag" data-task="${t.id}" style="cursor:pointer">${t.id}</span> ${esc(t.title)}<div class="muted" style="font-size:12.5px">Supervisor suggests you review this: ${esc(t.review_suggest.reason)}</div>
      <button class="btn sm primary" data-rsug="accept" data-t="${t.id}">I'll review it</button> <button class="btn sm" data-rsug="decline" data-t="${t.id}">You decide</button></div>`).join("")}
  </div></div>`;
}
```

- [ ] **Step 2: Suggestions in the plan-approval panel**

In `approveBanner()`, build and insert before the `Check the timeline` line (inside the returned template's left `<div>`):

```js
  const sugg=pt.filter(t=>t.review_suggest?.state==="pending"&&aiTask(t));
  const suggHtml=sugg.length?`<div style="margin-top:10px"><div class="label">Supervisor suggests you review these reports</div>${sugg.map(t=>`<div style="margin-top:6px"><b>${t.id}</b> ${esc(t.title)}<div class="muted" style="font-size:12.5px">${esc(t.review_suggest.reason)}</div><button class="btn sm primary" data-rsug="accept" data-t="${t.id}">I'll review it</button> <button class="btn sm" data-rsug="decline" data-t="${t.id}">Keep with supervisor</button></div>`).join("")}</div>`:"";
```

and place `${suggHtml}` right after `${which}`. (`pt` is already defined in that function.)

- [ ] **Step 3: Phase checkbox and queue in Timeline, badge on Board**

In `viewPhases`, in the `strip` chip template, after the `.bar` div add:

```js
${(()=>{ const ai=ts_.filter(t=>aiTask(t)&&!["done","cancelled"].includes(t.status)); if(!ai.length) return ""; return `<label class="muted" style="font-size:12px;display:flex;gap:6px;margin-top:6px;cursor:pointer"><input type="checkbox" data-phrev="${ai.map(t=>t.id).join(",")}" ${ai.every(isUserRev)?"checked":""}>I'll review this phase</label>`; })()}
```

Change the returned string `approveBanner() + prBanner()` to `approveBanner() + reviewQueue() + prBanner()`. In `viewBoard`, change `folderBanner() + msBar(st,true)` to `reviewQueue() + folderBanner() + msBar(st,true)`, and in the card `.meta` row add `${isUserRev(t)?'<span class="tag" style="color:var(--purple)">you review</span>':""}` after `${execTag(t)}`.

- [ ] **Step 4: Drawer controls**

In `renderDrawer`, after the Acceptance criteria `.sec` line, add:

```js
     ${aiTask(t)&&!["done","cancelled"].includes(t.status)?`<div class="sec"><div class="label">Who reviews the report</div><select class="sel" id="revSel" data-t="${t.id}"><option value="supervisor" ${isUserRev(t)?"":"selected"}>Supervisor</option><option value="user" ${isUserRev(t)?"selected":""}>Me</option></select></div>`:""}
     ${t.status==="review"&&isUserRev(t)?`<div class="sec" style="border:1px solid var(--purple);border-radius:10px;padding:12px"><div class="label" style="color:var(--purple)">Your review</div>
       <textarea id="revMsg" rows="4" style="width:100%;margin:8px 0" placeholder="Your feedback (required to request changes)"></textarea>
       <button class="btn sm primary" data-rv="approve" data-t="${t.id}">✓ Approve</button> <button class="btn sm danger" data-rv="changes" data-t="${t.id}">Request changes</button></div>`:""}
```

Because the drawer only re-renders when `changed("drawer",[...])` sees new data, the textarea is not wiped while typing. If it is, store the text in `S.drafts` the way the chat box does.

- [ ] **Step 5: Click and change handlers**

In the big `e.target.closest("[data-view],...")` selector string, add `[data-rv],[data-rsug],[data-phrev]` before `[data-task]`. In the handler chain (next to the `openVsc` line), add:

```js
    if(el.dataset.rv){ const msg=(document.getElementById("revMsg")?.value||"").trim();
      if(el.dataset.rv==="changes"&&!msg){ toast("Write what should change", true); return; }
      try{ await post("/api/review",{t:el.dataset.t,verdict:el.dataset.rv,message:msg}); toast(el.dataset.rv==="approve"?"Approved":"Sent back with your feedback"); }
      catch(err){ toast(err.message||"Couldn't save the review", true); return; }
      await tick(); return; }
    if(el.dataset.rsug){ await post("/api/review-suggest",{t:el.dataset.t,accept:el.dataset.rsug==="accept"}); toast(el.dataset.rsug==="accept"?"You'll review it":"Supervisor will decide"); await tick(); return; }
    if(el.dataset.phrev){ await post("/api/reviewer",{tasks:el.dataset.phrev.split(","),reviewer:el.checked?"user":"supervisor"}); toast(el.checked?"You'll review this phase":"Supervisor reviews this phase"); await tick(); return; }
```

Because the earlier `[data-task]` entry would also catch buttons with both `data-task` and other attributes, keep the `Review` button in `reviewQueue` as `data-task` only (as written). In the change handler (near `if(e.target.id==="execSel"){`), add:

```js
  if(e.target.id==="revSel"){ await post("/api/reviewer",{tasks:[e.target.dataset.t],reviewer:e.target.value}); toast(e.target.value==="user"?"You'll review this one":"Supervisor reviews this one"); await tick(); return; }
```

(If that handler is not `async`, make it so; check how the `execSel` branch awaits `post`.)

- [ ] **Step 6: Commit**

```bash
git add dashboard/index.html
git commit -m "feat: review queue, drawer review box, phase checkbox and suggestions in the dashboard"
```

---

### Task 5: Docs and end-to-end check in the browser

**Files:**
- Modify: `README.md` (feature section near the "Open folder" paragraph ~line 311 or the dashboard section), `SKILL.md` (supervisor guidance: reviewer field, `mc reviewer`, `mc suggest-review`).

- [ ] **Step 1: Document**

Add a short README section "Reviewing reports yourself" (what the checkbox/select do, that user-reviewed tasks never auto-approve and don't wake the supervisor, the two CLI commands, `review_suggest` in plan JSON). Add the same three facts to `SKILL.md` next to the other supervisor commands.

- [ ] **Step 2: Build a throwaway project and start the dashboard from this checkout**

```bash
mkdir -p /tmp/mc-e2e && cd /tmp/mc-e2e && python3 "/Users/ali/Documents/GitHub/Mission Control/mission-control/scripts/mc.py" init
```

Then write a two-task plan with a dependency and a `review_suggest` on one task, load it with `mc plan`, and start the dashboard from this checkout (`python3 scripts/dashboard.py --port 7799`, in the background; do not touch the user's port 7777 server). Use the exact `init`/`plan` flags from `mc.py --help` if the defaults differ.

- [ ] **Step 3: Verify in the browser pane**

Open `http://127.0.0.1:7799/`, and check:
1. Plan-approval panel lists the suggestion; Accept sets the task's `reviewer` to `user` (`cat .mission/tasks/T-00x.json`).
2. The Timeline phase checkbox toggles `reviewer` for every task in that phase; the drawer select overrides one task.
3. Set a task to `status: "review"` by editing its JSON; with `reviewer: user` it appears under "Needs your review", the drawer shows Approve / Request changes, Approve marks it `done`, Request changes without text is blocked with a toast, and with text writes `.mission/feedback/T-00x.md` and sets `needs_changes`.
4. A `reviewer: supervisor` task in `review` with a pending suggestion shows "I'll review it / You decide".

- [ ] **Step 4: Run the unit tests once more and stop the throwaway server**

Run: `cd scripts && python3 -m unittest test_user_review -v 2>&1 | tail -5`
Expected: all pass. Stop the port-7799 server and remove `/tmp/mc-e2e`.

- [ ] **Step 5: Commit**

```bash
git add README.md SKILL.md
git commit -m "docs: reviewing reports yourself"
```

---

## Self-Review

- **Spec coverage:** data model (T1), user-reviewer flow incl. no wake / no auto-approve (T1), `/api/review` + drawer buttons (T3, T4), choosing reviewer per task and phase incl. CLI (T2, T3, T4), plan-time and report-time suggestions (T1, T2, T4), supervisor prompt rule (T2), edge cases (final/human skipped, back-to-supervisor wakes: T1), tests (T1, T2, T5), docs (T5). The documented deviations are listed under Global Constraints.
- **Placeholders:** none; the two spots that depend on exact names in existing code (`inbox_items`, `post` signature) have a concrete lookup command or instruction.
- **Type consistency:** `reviewer_of`, `set_reviewer(m, ids, reviewer, by)`, `suggest_review(m, tid, reason)`, `answer_suggestion(m, tid, accept)`, `phase_task_ids(m, phase)` are used with the same names and signatures across tasks; JSON fields `reviewer` and `review_suggest.{reason,state,at}` are the same in code, API and UI.
