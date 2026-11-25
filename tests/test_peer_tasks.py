from native_team.peer_tasks import PeerTasks
from native_team.peer_decisions import PeerDecisions
from native_team.store import TeamError
from test_peers import PeerFixture


class PeerTaskTests(PeerFixture):
    def test_stranded_writer_requires_explicit_recovery_and_old_result_is_fenced(self):
        task = self.tasks.create(
            self.members["agy1"],
            self.team["id"],
            "code2",
            "agy2",
            "Read",
            "Evidence",
            "recovery",
        )
        self.sync("code2")
        claim = self.tasks.take(self.members["code2"], self.team["id"])
        self.peers.update_member(
            self.members["code2"], self.team["id"], "code2", 1, state="inactive"
        )
        self.sync("agy1")
        for instruction, evidence in [("", "Stopped"), ("Recover", "")]:
            with self.assertRaises(TeamError):
                self.tasks.recover(
                    self.members["agy1"],
                    task["id"],
                    claim["revision"],
                    instruction,
                    evidence,
                )
        recovered = self.tasks.recover(
            self.members["agy1"],
            task["id"],
            claim["revision"],
            "User authorizes recovery after closing the writer",
            "Native process exit confirmed by user",
        )
        with self.assertRaises(TeamError):
            self.tasks.report(
                self.members["code2"], task["id"], claim["revision"], "Stale", "Stale"
            )
        changed = self.tasks.handoff(
            self.members["agy1"],
            task["id"],
            recovered["revision"],
            owner_alias="code3",
            reason="Continue from preserved evidence",
        )
        self.sync("code3")
        claimed = self.tasks.take(self.members["code3"], self.team["id"])
        self.assertEqual(claimed["id"], changed["id"])

    def test_inactive_decision_participant_cannot_be_silently_excluded(self):
        decision = self.decisions.propose(
            self.members["agy1"],
            self.team["id"],
            "Contract",
            "A",
            ["code2"],
            "omission",
        )
        self.peers.update_member(
            self.members["code2"], self.team["id"], "code2", 1, state="inactive"
        )
        self.sync("agy1")
        with self.assertRaises(TeamError):
            self.decisions.propose(
                self.members["agy1"],
                self.team["id"],
                "Contract",
                "B",
                ["code3"],
                "replacement",
                supersedes=decision["id"],
            )
        replacement = self.decisions.propose(
            self.members["agy1"],
            self.team["id"],
            "Contract",
            "B",
            ["code3"],
            "replacement",
            supersedes=decision["id"],
            user_instruction="User moves the inactive workstream to code3",
        )
        self.sync("code3")
        self.assertEqual(
            self.decisions.respond(
                self.members["code3"],
                replacement["id"],
                1,
                "agree",
                "Reviewed the original objection and the revised contract",
            )["state"],
            "agreed",
        )

    def test_team_closure_preserves_history_and_requires_no_outstanding_claims(self):
        task = self.tasks.create(
            self.members["agy1"],
            self.team["id"],
            "code2",
            "agy2",
            "Read",
            "Evidence",
            "closure",
        )
        with self.assertRaises(TeamError):
            self.peers.set_state(
                self.members["agy1"],
                self.team["id"],
                1,
                "done",
                "Finish the team",
                "Checks pass",
            )
        self.tasks.cancel(
            self.members["agy1"], task["id"], task["revision"], "User drops this task"
        )
        done = self.peers.set_state(
            self.members["agy1"],
            self.team["id"],
            1,
            "done",
            "Finish this team",
            "All scoped work reconciled",
        )
        self.assertEqual(done["state"], "done")
        self.assertEqual(
            self.peers.sync(self.members["code2"], self.team["id"])["action"], "done"
        )
        self.assertEqual(
            self.tasks.get(self.members["code2"], task["id"])["task"]["state"],
            "cancelled",
        )

    def test_inactive_executor_does_not_prevent_reviewer_only_handoff(self):
        task = self.tasks.create(
            self.members["agy1"],
            self.team["id"],
            "code2",
            "agy2",
            "Read",
            "Evidence",
            "late-review",
        )
        self.sync("code2")
        claimed = self.tasks.take(self.members["code2"], self.team["id"])
        self.peers.update_member(
            self.members["code2"], self.team["id"], "code2", 1, state="inactive"
        )
        report = self.tasks.report(
            self.members["code2"],
            task["id"],
            claimed["revision"],
            "Late result",
            "Evidence",
        )
        self.peers.update_member(
            self.members["agy2"], self.team["id"], "agy2", 1, state="inactive"
        )
        self.sync("agy1")
        changed = self.tasks.handoff(
            self.members["agy1"],
            task["id"],
            report["revision"],
            reviewer_alias="code3",
            reason="Review remains useful",
        )
        self.assertEqual(changed["state"], "review")
        self.assertEqual(changed["owner"], self.members["code2"])
        self.sync("code3")
        self.assertEqual(
            self.tasks.review(
                self.members["code3"],
                task["id"],
                changed["revision"],
                "accept",
                "Verified",
            )["state"],
            "accepted",
        )

    def test_competing_replacements_cannot_both_become_current(self):
        first = self.decisions.propose(
            self.members["agy1"], self.team["id"], "Contract", "A", ["code2"], "first"
        )
        replacements = [
            self.decisions.propose(
                self.members["agy1"],
                self.team["id"],
                "Contract",
                text,
                ["code2"],
                text,
                supersedes=first["id"],
            )
            for text in ("B", "C")
        ]
        self.sync("code2")
        self.decisions.respond(
            self.members["code2"], replacements[0]["id"], 1, "agree", "B works"
        )
        self.assertEqual(
            self.decisions.get(self.members["agy1"], replacements[1]["id"])["state"],
            "superseded",
        )
        self.sync("code2")
        with self.assertRaises(TeamError):
            self.decisions.respond(
                self.members["code2"],
                replacements[1]["id"],
                1,
                "agree",
                "Cannot also replace A with C",
            )

    def setUp(self):
        super().setUp()
        self.tasks = PeerTasks(self.peers)
        self.decisions = PeerDecisions(self.peers)
        self.sync_all()

    def sync_all(self):
        for alias in self.members:
            self.sync(alias)

    def create(self, key="task", owner="code2", reviewer="agy2", paths=None):
        return self.tasks.create(
            self.members["agy1"],
            self.team["id"],
            owner,
            reviewer,
            "Read fixture",
            "Verifiable answer",
            key,
            paths,
        )

    def report(self, owner="code2"):
        task = self.tasks.take(self.members[owner], self.team["id"])
        return self.tasks.report(
            self.members[owner],
            task["id"],
            task["revision"],
            "Answer",
            "Fixture evidence",
        )

    def test_any_provider_executes_and_only_designated_independent_peer_accepts(self):
        task = self.create()
        report = self.report()
        for wrong in ("agy1", "code2", "code4"):
            with self.assertRaises(TeamError):
                self.tasks.review(
                    self.members[wrong],
                    task["id"],
                    report["revision"],
                    "accept",
                    "Guess",
                )
        accepted = self.tasks.review(
            self.members["agy2"],
            task["id"],
            report["revision"],
            "accept",
            "Independently verified",
        )
        self.assertEqual(accepted["state"], "accepted")
        later = self.tasks.create(
            self.members["code3"],
            self.team["id"],
            "agy2",
            "code4",
            "Follow-on",
            "Evidence",
            "follow-on",
        )
        report = self.report("agy2")
        self.assertEqual(report["id"], later["id"])
        self.tasks.review(
            self.members["code4"], later["id"], report["revision"], "accept", "Checked"
        )
        with self.assertRaises(TeamError):
            self.create("self-review", owner="agy1", reviewer="agy1")

    def test_claims_require_current_context_and_repeated_take_resumes_same_claim(self):
        task = self.create()
        self.peers.update_member(
            self.members["agy2"], self.team["id"], "agy2", 1, lane="Independent review"
        )
        with self.assertRaises(TeamError):
            self.tasks.take(self.members["code2"], self.team["id"])
        self.sync("code2")
        first = self.tasks.take(self.members["code2"], self.team["id"])
        second = self.tasks.take(self.members["code2"], self.team["id"])
        self.assertEqual(first, second)
        self.assertEqual(first["id"], task["id"])

    def test_inactive_executor_keeps_write_reservation_and_can_preserve_late_evidence(
        self,
    ):
        self.peers.update_member(
            self.members["code2"], self.team["id"], "code2", 1, paths=["src"]
        )
        self.sync_all()
        task = self.create(paths=["src/module.py"])
        claimed = self.tasks.take(self.members["code2"], self.team["id"])
        self.peers.update_member(
            self.members["agy1"],
            self.team["id"],
            "code2",
            2,
            state="inactive",
            user_instruction="User paused this executor",
        )
        with self.assertRaises(TeamError):
            self.peers.update_member(
                self.members["code3"], self.team["id"], "code3", 1, paths=["src"]
            )
        self.sync("agy1")
        with self.assertRaises(TeamError):
            self.tasks.handoff(
                self.members["agy1"],
                task["id"],
                claimed["revision"],
                owner_alias="code3",
                reason="Try taking an active claim",
            )
        late = self.tasks.report(
            self.members["code2"],
            task["id"],
            claimed["revision"],
            "Finished before pause",
            "Preserved evidence",
        )
        self.assertEqual(late["state"], "review")
        with self.assertRaises(TeamError):
            self.tasks.take(self.members["code2"], self.team["id"])
        self.peers.update_member(
            self.members["code3"], self.team["id"], "code3", 1, paths=["src"]
        )

    def test_inactive_reviewer_is_visible_and_replacement_does_not_strand_review(self):
        self.create()
        self.peers.update_member(
            self.members["agy2"], self.team["id"], "agy2", 1, state="inactive"
        )
        self.sync_all()
        report = self.report()
        self.assertEqual(report["reviewer_handoff_needed"], 1)
        handed = self.tasks.handoff(
            self.members["code2"],
            report["id"],
            report["revision"],
            reviewer_alias="code3",
            reason="Review peer paused; request independent code3 review",
        )
        self.assertEqual(handed["state"], "review")
        with self.assertRaises(TeamError):
            self.tasks.review(
                self.members["code3"],
                handed["id"],
                handed["revision"],
                "accept",
                "Before context sync",
            )
        self.sync("code3")
        self.assertEqual(
            self.tasks.review(
                self.members["code3"],
                handed["id"],
                handed["revision"],
                "accept",
                "Verified preserved evidence",
            )["state"],
            "accepted",
        )

    def test_explicit_release_then_handoff_preserves_history_and_rejects_stale_result(
        self,
    ):
        task = self.create()
        claimed = self.tasks.take(self.members["code2"], self.team["id"])
        released = self.tasks.release(
            self.members["code2"],
            task["id"],
            claimed["revision"],
            "Stopping writes",
            "Partial source reference",
        )
        handed = self.tasks.handoff(
            self.members["agy1"],
            task["id"],
            released["revision"],
            owner_alias="code3",
            reason="Continue from checkpoint",
        )
        with self.assertRaises(TeamError):
            self.tasks.report(
                self.members["code2"],
                task["id"],
                claimed["revision"],
                "Late",
                "Old result",
            )
        self.sync("code3")
        self.assertEqual(
            self.tasks.take(self.members["code3"], self.team["id"])["owner"],
            self.members["code3"],
        )
        record = self.tasks.get(self.members["agy2"], task["id"])
        self.assertEqual(
            [h["kind"] for h in record["history"]],
            ["assigned", "claimed", "released", "handoff", "claimed"],
        )
        self.assertEqual(handed["evidence"], "Partial source reference")

    def test_revision_rounds_are_bounded(self):
        self.create()
        for _ in range(2):
            report = self.report()
            self.tasks.review(
                self.members["agy2"],
                report["id"],
                report["revision"],
                "revise",
                "Concrete missing evidence",
            )
        report = self.report()
        with self.assertRaises(TeamError):
            self.tasks.review(
                self.members["agy2"],
                report["id"],
                report["revision"],
                "revise",
                "Endless debate",
            )
        self.assertEqual(
            self.tasks.review(
                self.members["agy2"],
                report["id"],
                report["revision"],
                "block",
                "Needs a substantive decision",
            )["state"],
            "blocked",
        )

    def test_message_ack_is_not_agreement_and_one_peer_cannot_override_an_objection(
        self,
    ):
        decision = self.decisions.propose(
            self.members["agy1"],
            self.team["id"],
            "Interface",
            "Use the proposed contract",
            ["agy2", "code2"],
            "contract",
        )
        self.sync_all()
        self.assertEqual(
            self.decisions.get(self.members["code3"], decision["id"])["state"],
            "proposed",
        )
        with self.assertRaises(TeamError):
            self.decisions.respond(
                self.members["agy1"], decision["id"], 1, "agree", "I initiated it"
            )
        dispute = self.decisions.respond(
            self.members["agy2"],
            decision["id"],
            1,
            "object",
            "Missing failure contract",
        )
        self.assertEqual(dispute["state"], "disputed")
        still_disputed = self.decisions.respond(
            self.members["code2"],
            decision["id"],
            dispute["revision"],
            "agree",
            "My portion is sound",
        )
        self.assertEqual(still_disputed["state"], "disputed")
        agreed = self.decisions.respond(
            self.members["agy2"],
            decision["id"],
            still_disputed["revision"],
            "agree",
            "Failure contract clarified",
        )
        self.assertEqual(agreed["state"], "agreed")
        self.assertTrue(
            self.peers.sync(self.members["code3"], self.team["id"])["sync_required"]
        )

    def test_replacement_cannot_exclude_a_previously_affected_peer(self):
        first = self.decisions.propose(
            self.members["agy1"],
            self.team["id"],
            "Contract",
            "Initial proposal",
            ["agy2", "code2"],
            "initial",
        )
        with self.assertRaises(TeamError):
            self.decisions.propose(
                self.members["code3"],
                self.team["id"],
                "Contract",
                "Ignore one peer",
                ["code2"],
                "bad-replacement",
                first["id"],
            )
