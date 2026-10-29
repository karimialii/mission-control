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

    def test_phase_selection_uses_dependency_levels(self):
        a = add_task(self.m, status="todo")
        b = add_task(self.m, status="todo", depends_on=[a])
        ids = mc.phase_task_ids(self.m, 1)
        self.assertEqual(ids, [b])


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
