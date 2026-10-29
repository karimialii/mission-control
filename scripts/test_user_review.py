import os
import sys
import tempfile
from unittest import mock
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


def peek_after_commit(m):
    """Move the supervisor cursor past everything so far, then return only events added afterwards."""
    mc.supervisor_cursor_commit(m, mc.events_total(m))
    return lambda: mc.supervisor_events(m, advance=False)


class HandbackTests(unittest.TestCase):
    def setUp(self):
        self.m = make_mission()

    def test_set_reviewer_back_to_supervisor_wakes_loop(self):
        tid = add_task(self.m)
        mc.set_reviewer(self.m, [tid], "user")
        peek = peek_after_commit(self.m)
        mc.set_reviewer(self.m, [tid], "supervisor")
        self.assertTrue(mc.needs_decision(self.m, peek()))

    def test_declined_suggestion_wakes_loop(self):
        tid = add_task(self.m)
        mc.suggest_review(self.m, tid, "risky")
        peek = peek_after_commit(self.m)
        mc.answer_suggestion(self.m, tid, False)
        self.assertTrue(mc.needs_decision(self.m, peek()))

    def test_handback_of_non_review_task_adds_no_note(self):
        tid = add_task(self.m, status="todo")
        mc.set_reviewer(self.m, [tid], "user")
        peek = peek_after_commit(self.m)
        mc.set_reviewer(self.m, [tid], "supervisor")
        self.assertFalse(mc.needs_decision(self.m, peek()))


class SupervisorGuardTests(unittest.TestCase):
    def setUp(self):
        self.m = make_mission()
        self.tid = add_task(self.m)
        mc.set_reviewer(self.m, [self.tid], "user")
        self.patch = mock.patch.dict(os.environ, {"MC_ROLE": "supervisor"})
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.cwd = mock.patch.object(mc, "need_mission", return_value=self.m)
        self.cwd.start()
        self.addCleanup(self.cwd.stop)

    def ns(self, **kw):
        import argparse
        return argparse.Namespace(**kw)

    def test_supervisor_cannot_review_user_task(self):
        with self.assertRaises(SystemExit):
            mc.cmd_review(self.ns(task=self.tid, verdict="approve", message=[]))
        self.assertEqual(self.m.task(self.tid)["status"], "review")

    def test_supervisor_cannot_change_reviewer(self):
        with self.assertRaises(SystemExit):
            mc.cmd_reviewer(self.ns(first=self.tid, second="supervisor", phase=None))
        self.assertEqual(mc.reviewer_of(self.m.task(self.tid)), "user")

    def test_user_cmd_reviewer_is_attributed_to_user(self):
        with mock.patch.dict(os.environ, {"MC_ROLE": ""}):
            mc.cmd_reviewer(self.ns(first=self.tid, second="supervisor", phase=None))
        self.assertEqual(mc.reviewer_of(self.m.task(self.tid)), "supervisor")
        self.assertEqual([e for e in self.m.events() if e["kind"] == "task_updated"][-1]["actor"], "user")

    def test_supervisor_set_ignores_reviewer_keys(self):
        other = add_task(self.m)
        mc.cmd_set(self.ns(task=other, pairs=["reviewer=user", "review_suggest=1", "title=new"]))
        t = self.m.task(other)
        self.assertEqual(t["title"], "new")
        self.assertNotEqual(t.get("reviewer"), "user")
        self.assertFalse(t.get("review_suggest"))

    def test_state_text_labels_user_reviewed(self):
        text = mc.state_text(self.m)
        line = [l for l in text.splitlines() if "Awaiting your review" in l][0]
        self.assertNotIn(self.tid, line)

    def test_reviewer_phase_with_no_tasks_dies(self):
        with mock.patch.dict(os.environ, {"MC_ROLE": ""}):
            with self.assertRaises(SystemExit):
                mc.cmd_reviewer(self.ns(first="user", second=None, phase=9))


class PendingSuggestionInboxTests(unittest.TestCase):
    def test_pending_suggestion_is_fyi_not_review(self):
        m = make_mission()
        tid = add_task(m)
        mc.suggest_review(m, tid, "touches auth")
        joined = "\n".join(mc.inbox_items(m, mark_read=False))
        self.assertNotIn(f"REVIEW {tid}", joined)
        self.assertIn("don't review it", joined)


class CheckPassGuardTests(unittest.TestCase):
    def test_passing_check_keeps_user_reviewed_task_in_review(self):
        m = make_mission()
        tid = add_task(m, status="running", check="true")
        mc.set_reviewer(m, [tid], "user")
        m.update_task(tid, lambda x: x.update(run={"attempt": 1}))
        mc.finalize(m, tid, 1, {"status": "done", "summary": "ok"}, {}, 0, True, workdir=m.root)
        t = m.task(tid)
        self.assertEqual((t["status"], t["step"]), ("review", "awaiting your review"))


class NotifyClassifyTests(unittest.TestCase):
    def test_review_suggest_is_decision(self):
        self.assertEqual(mc.notify.classify({"kind": "review_suggest"}), "decision")

    def test_report_for_user_review_is_decision(self):
        m = make_mission()
        tid = add_task(m)
        ev = {"kind": "report", "task": tid}
        self.assertIsNone(mc.notify.classify(ev, m))
        mc.set_reviewer(m, [tid], "user")
        self.assertEqual(mc.notify.classify(ev, m), "decision")


class NeedsYouTests(unittest.TestCase):
    def test_summary_counts_reviews(self):
        import dashboard
        m = make_mission()
        a = add_task(m)
        b = add_task(m)
        add_task(m)
        mc.set_reviewer(m, [a], "user")
        mc.suggest_review(m, b, "risky")
        s = dashboard.summary(str(m.root), {})
        self.assertEqual(s["needs_you"], 2)


if __name__ == "__main__":
    unittest.main()
