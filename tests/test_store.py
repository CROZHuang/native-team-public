from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import tempfile
import unittest
from unittest.mock import patch
import uuid

from native_team.store import Store, TeamError


class WorkControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "state")
        self.lead = self.store.join("codex", "lead", str(self.root), str(uuid.uuid4()))
        self.adviser = self.store.join("agy", "adviser", str(self.root))
        self.work = self.store.start(
            self.lead["id"],
            "Explore the design",
            "Evidence from README",
            "Review, then ask the follow-on question",
            "work-1",
        )

    def assign(self, key="step-1"):
        return self.store.assign(
            self.lead["id"],
            self.work["id"],
            "adviser",
            "Find the startup command",
            "Cite the README",
            key,
        )

    def report(self, task):
        claimed = self.store.take(self.adviser["id"])
        self.assertEqual(claimed["id"], task["id"])
        return self.store.report(
            self.adviser["id"],
            task["id"],
            claimed["revision"],
            "Found the startup command",
            "README: Startup",
        )

    def test_revision_then_later_assignment_requires_coordinator_acceptance(self):
        task = self.assign()
        review = self.report(task)
        self.assertEqual(
            self.store.status(self.lead["id"])["action"], "review_required"
        )
        with self.assertRaises(TeamError):
            self.store.decide(
                self.adviser["id"], task["id"], review["revision"], "accept", "Good"
            )
        revised = self.store.decide(
            self.lead["id"],
            task["id"],
            review["revision"],
            "revise",
            "Also explain restart",
        )
        self.assertEqual(revised["state"], "ready")
        review = self.report(revised)
        self.store.decide(
            self.lead["id"], task["id"], review["revision"], "accept", "Verified README"
        )
        self.assertEqual(
            self.store.status(self.lead["id"])["action"], "next_step_required"
        )
        later = self.assign("step-2")
        review = self.report(later)
        self.store.decide(
            self.lead["id"],
            later["id"],
            review["revision"],
            "accept",
            "Verified follow-on",
        )
        current = self.store.status(self.lead["id"])["work"][0]
        done = self.store.update_work(
            self.lead["id"],
            current["id"],
            current["revision"],
            "done",
            "Both questions verified",
            "README checked independently",
        )
        self.assertEqual(done["state"], "done")
        self.assertIsNone(self.store.take(self.adviser["id"]))

    def test_idempotency_and_stale_results(self):
        task = self.assign()
        self.assertEqual(self.assign()["id"], task["id"])
        claimed = self.store.take(self.adviser["id"])
        self.assertEqual(self.store.take(self.adviser["id"]), claimed)
        review = self.store.report(
            self.adviser["id"], task["id"], claimed["revision"], "Result", "Evidence"
        )
        with self.assertRaises(TeamError):
            self.store.report(
                self.adviser["id"],
                task["id"],
                claimed["revision"],
                "Different result",
                "Evidence",
            )
        self.assertEqual(
            self.store.status(self.lead["id"])["assignments"][0]["revision"],
            review["revision"],
        )

    def test_rejects_cross_project_and_completion_with_unaccepted_work(self):
        other = self.root / "other"
        other.mkdir()
        outsider = self.store.join("codex", "other-lead", str(other), str(uuid.uuid4()))
        with self.assertRaises(TeamError):
            self.store.assign(
                outsider["id"], self.work["id"], "adviser", "Work", "Evidence", "bad"
            )
        self.assign()
        with self.assertRaises(TeamError):
            self.store.update_work(
                self.lead["id"],
                self.work["id"],
                self.work["revision"],
                "done",
                "Finished",
                "Claimed complete",
            )

    def test_pause_resume_and_cancel_preserve_history_and_prevent_work(self):
        task = self.assign()
        paused = self.store.update_work(
            self.lead["id"], self.work["id"], 1, "paused", "User paused"
        )
        self.assertIsNone(self.store.take(self.adviser["id"]))
        active = self.store.update_work(
            self.lead["id"], paused["id"], paused["revision"], "active", "User resumed"
        )
        claimed = self.store.take(self.adviser["id"])
        self.store.update_work(
            self.lead["id"],
            active["id"],
            active["revision"],
            "cancelled",
            "User cancelled",
        )
        with self.assertRaises(TeamError):
            self.store.report(
                self.adviser["id"],
                task["id"],
                claimed["revision"],
                "Too late",
                "Evidence",
            )
        self.assertEqual(
            self.store.status(self.lead["id"])["assignments"][0]["state"], "cancelled"
        )

    def test_review_rounds_are_bounded(self):
        task = self.assign()
        for _ in range(2):
            review = self.report(task)
            task = self.store.decide(
                self.lead["id"],
                task["id"],
                review["revision"],
                "revise",
                "Concrete correction",
            )
        review = self.report(task)
        with self.assertRaisesRegex(TeamError, "revision limit"):
            self.store.decide(
                self.lead["id"], task["id"], review["revision"], "revise", "More"
            )

    def test_restart_recovers_work_and_never_resends_uncertain_notification(self):
        self.report(self.assign())
        notice = self.store.reserve_notice(self.lead["id"])
        self.assertIsNotNone(notice)
        reopened = Store(self.root / "state")
        reopened.recover_notices(self.lead["id"])
        self.assertIsNone(reopened.reserve_notice(self.lead["id"]))
        status = reopened.status(self.lead["id"])
        self.assertEqual(status["action"], "review_required")
        self.assertEqual(status["notifications"][0]["state"], "uncertain")

    def test_queue_acceptance_is_not_receipt_or_completion(self):
        self.report(self.assign())
        notice = self.store.reserve_notice(self.lead["id"])
        self.store.notice_result(notice["id"], "queued", "Native queue accepted")
        status = self.store.status(self.lead["id"])
        self.assertIsNone(status["notifications"][0]["received_at"])
        received = self.store.receive(self.lead["id"])
        self.assertIsNotNone(received["notifications"][0]["received_at"])
        self.assertEqual(received["action"], "review_required")

    def test_compact_receipt_preserves_closed_notices_and_parked_work(self):
        lead, adviser = self.lead["id"], self.adviser["id"]
        old = self.assign()
        self.report(old)
        sent = self.store.reserve_notice(lead)
        self.store.notice_result(
            sent["id"], "queued", "Native queue accepted before closure"
        )
        self.store.update_work(lead, self.work["id"], 1, "cancelled", "Park archive")
        paused = self.store.start(lead, "Paused", "Check", "Wait", "paused")
        self.store.update_work(lead, paused["id"], 1, "paused", "Pause requested")
        active = self.store.start(lead, "Active", "Check", "Review", "active")
        task = self.store.assign(
            lead, active["id"], "adviser", "Inspect", "Check", "new"
        )
        self.report(task)
        compact = self.store.receive(lead, include_closed=False)
        self.assertEqual(compact["action"], "review_required")
        self.assertEqual([w["id"] for w in compact["work"]], [active["id"]])
        self.assertEqual([a["id"] for a in compact["assignments"]], [task["id"]])
        self.assertEqual(
            compact["parked_work"],
            [{"id": paused["id"], "state": "paused", "pending_notices": 0}],
        )
        self.assertEqual(
            (compact["closed_work_count"], compact["closed_assignment_count"]), (1, 1)
        )
        self.assertEqual(compact["pending_closed_work_ids"], [self.work["id"]])
        self.assertEqual({e["work_id"] for e in compact["events"]}, {active["id"]})
        self.assertEqual(
            [n["work_id"] for n in compact["notifications"]], [active["id"]]
        )
        self.assertIsNotNone(compact["notifications"][0]["received_at"])
        hidden = next(
            n
            for n in self.store.status(lead)["notifications"]
            if n["work_id"] == self.work["id"]
        )
        self.assertIsNone(hidden["received_at"])
        full = self.store.receive(lead)
        self.assertEqual(len(full["work"]), 3)
        self.assertTrue(all(n["received_at"] for n in full["notifications"]))
        self.assertFalse(
            self.store.status(lead, include_closed=False).get("pending_closed_work_ids")
        )
        self.assertFalse(
            self.store.status(adviser, include_closed=False).get("parked_work")
        )

    def test_cancelled_unsent_notice_is_historical_only(self):
        lead = self.lead["id"]
        self.report(self.assign())
        self.store.update_work(
            lead, self.work["id"], 1, "cancelled", "Cancelled before dispatch"
        )
        compact = self.store.receive(lead, include_closed=False)
        self.assertFalse(compact.get("pending_closed_work_ids"))
        (notice,) = self.store.status(lead)["notifications"]
        self.assertEqual(notice["state"], "suppressed")
        self.assertIsNone(notice["received_at"])

    def test_unread_notice_stays_visible_while_work_is_parked(self):
        lead = self.lead["id"]
        self.report(self.assign())
        revision = 1
        for state in ("paused", "blocked"):
            work = self.store.update_work(
                lead, self.work["id"], revision, state, "Waiting on decision"
            )
            revision = work["revision"]
            compact = self.store.receive(lead, include_closed=False)
            self.assertEqual(compact["action"], "idle")
            self.assertEqual(compact["notifications"], [])
            self.assertEqual(
                compact["parked_work"],
                [{"id": work["id"], "state": state, "pending_notices": 1}],
            )
            self.assertEqual(
                self.store.status(self.adviser["id"], include_closed=False)[
                    "parked_work"
                ][0]["pending_notices"],
                0,
            )
        (notice,) = self.store.receive(lead)["notifications"]
        self.assertIsNotNone(notice["received_at"])
        self.assertEqual(
            self.store.status(lead, include_closed=False)["parked_work"][0][
                "pending_notices"
            ],
            0,
        )

    def test_compact_counts_and_event_window_respect_the_actors_scope(self):
        lead, adviser = self.lead["id"], self.adviser["id"]
        visible = self.assign()
        second = self.store.join("agy", "other-adviser", str(self.root))
        self.store.assign(
            lead, self.work["id"], second["alias"], "Other brief", "Check", "other"
        )
        review = self.report(visible)
        self.store.decide(lead, visible["id"], review["revision"], "accept", "Checked")
        active = self.store.status(adviser, include_closed=False)
        self.assertEqual(
            [(a["id"], a["state"]) for a in active["assignments"]],
            [(visible["id"], "accepted")],
        )
        self.store.update_work(lead, self.work["id"], 1, "cancelled", "Close fixture")
        unrelated = self.store.start(lead, "Unrelated", "Check", "Wait", "other-work")
        with self.store.transaction() as db:
            for i in range(90):
                self.store.event(db, self.work["id"], f"visible-{i}")
                self.store.event(db, unrelated["id"], f"private-{i}")
        compact = self.store.status(adviser, include_closed=False)
        self.assertEqual(compact["closed_assignment_count"], 1)
        self.assertEqual(
            self.store.status(lead, include_closed=False)["closed_assignment_count"], 2
        )
        self.assertEqual(self.store.status(adviser)["events"][0]["kind"], "visible-10")
        self.assertEqual(
            [e["kind"] for e in self.store.status(adviser)["events"]],
            [f"visible-{i}" for i in range(10, 90)],
        )

    def test_cannot_rebind_codex_identity(self):
        with self.assertRaises(TeamError):
            self.store.join("codex", "lead", str(self.root), str(uuid.uuid4()))

    def test_concurrent_native_pickup_resumes_one_claim(self):
        task = self.assign()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(lambda _: self.store.take(self.adviser["id"]), range(2))
            )
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0]["id"], task["id"])
        events = self.store.status(self.lead["id"])["events"]
        self.assertEqual(sum(e["kind"] == "claimed" for e in events), 1)

    def test_acknowledgement_without_decision_gets_bounded_continuation(self):
        self.report(self.assign())
        self.store.receive(self.lead["id"])
        stamp = self.store.status(self.lead["id"])["work"][0]["updated"]
        for attempt in range(1, 4):
            self.store.watch_work(self.lead["id"], at=stamp + 400 * attempt)
            if attempt <= 2:
                notice = self.store.reserve_notice(self.lead["id"])
                self.assertIsNotNone(notice)
                self.store.notice_result(notice["id"], "queued", "Fixture queue")
                self.store.receive(self.lead["id"])
            else:
                self.assertIsNone(self.store.reserve_notice(self.lead["id"]))
        self.assertEqual(self.store.status(self.lead["id"])["work"][0]["reminders"], 2)
        self.assertEqual(
            self.store.status(self.lead["id"])["action"], "review_required"
        )

    def test_watchdog_never_retries_unknown_delivery_or_paused_work(self):
        self.report(self.assign())
        stamp = self.work["updated"]
        notice = self.store.reserve_notice(self.lead["id"])
        self.store.notice_result(notice["id"], "uncertain", "Fixture timeout")
        self.store.watch_work(self.lead["id"], at=stamp + 1000)
        self.assertIsNone(self.store.reserve_notice(self.lead["id"]))
        self.store.receive(self.lead["id"])
        self.store.update_work(
            self.lead["id"], self.work["id"], 1, "paused", "User paused"
        )
        self.store.watch_work(self.lead["id"], at=stamp + 2000)
        self.assertIsNone(self.store.reserve_notice(self.lead["id"]))

    def test_delayed_receipt_starts_a_full_continuation_grace_period(self):
        task = self.assign()
        self.report(task)
        received_at = self.work["updated"] + 1000
        with patch("native_team.store.time.time", return_value=received_at):
            self.store.receive(self.lead["id"])
        self.store.watch_work(self.lead["id"], at=received_at + 1)
        self.assertIsNone(self.store.reserve_notice(self.lead["id"]))
        self.store.watch_work(self.lead["id"], at=received_at + 301)
        self.assertIsNotNone(self.store.reserve_notice(self.lead["id"]))


if __name__ == "__main__":
    unittest.main()
