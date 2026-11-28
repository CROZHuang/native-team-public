from contextlib import closing
from http.client import HTTPConnection
import json
import socket
import sqlite3
import threading
from unittest.mock import patch

from native_team.peer_tasks import PeerTasks
from native_team.peer_decisions import PeerDecisions
from native_team.store import Store, TeamError
from native_team.workbench_data import directory, team_detail, record_page
from native_team.workbench import make_server
from native_team.availability import Availability
from test_peers import PeerFixture


class WorkbenchFixture(PeerFixture):
    def setUp(self):
        super().setUp()
        self.sync("agy1")
        self.tasks = PeerTasks(self.peers)
        self.task = self.tasks.create(
            self.members["agy1"],
            self.team["id"],
            "code2",
            "agy2",
            "Check <script>literal content</script>",
            "Evidence required",
            "read-test",
        )
        self.peers.board(
            self.members["code2"],
            self.team["id"],
            0,
            "Current report",
            "Next action",
            "Awaiting evidence",
            "Review handoff",
        )
        self.decision = PeerDecisions(self.peers).propose(
            self.members["agy1"],
            self.team["id"],
            "Read boundary",
            "Preserve receipts",
            ["code2"],
            "read-decision",
        )
        other = self.root / "Other project"
        other.mkdir()
        a = self.store.join("agy", "other-a", other)
        self.store.join("agy", "other-b", other)
        closed = self.peers.create(
            a["id"],
            "Cancelled history",
            "Keep history",
            ["other-a", "other-b"],
            "closed",
        )
        self.peers.set_state(
            a["id"], closed["id"], 1, "cancelled", "User cancels fixture"
        )
        self.closed = closed
        self.closed_member = a["id"]

    def dump(self):
        with closing(self.store.connect()) as db:
            return list(db.iterdump())


class WorkbenchReadTests(WorkbenchFixture):
    def test_directory_attention_is_one_read_snapshot_and_archives_omit_cooldown(self):
        Availability(self.store).set(
            self.members["code2"], 0, "cooldown", "Fixture quota"
        )
        Availability(self.store).set(
            self.closed_member, 0, "cooldown", "Current unrelated quota"
        )
        with self.store.transaction() as db:
            db.execute(
                "UPDATE peer_tasks SET state='blocked',handoff_needed=1 WHERE id=?",
                (self.task["id"],),
            )
            db.execute(
                "UPDATE peer_deliveries SET state='uncertain' WHERE received_at IS NULL"
            )
        before = self.dump()
        rows = directory(self.store.state)["teams"]
        active = next(t for t in rows if t["id"] == self.team["id"])
        self.assertEqual(active["attention"]["decisions"], 1)
        self.assertEqual(active["attention"]["blocked_tasks"], 1)
        self.assertEqual(active["attention"]["handoff_tasks"], 1)
        self.assertEqual(active["attention"]["cooldowns"], 1)
        self.assertGreater(active["attention"]["deliveries"], 0)
        archived = next(t for t in rows if t["id"] == self.closed["id"])
        self.assertNotIn("cooldowns", archived["attention"])
        self.assertEqual(self.dump(), before)

    def test_workflow_shows_actual_task_revisions_and_recorded_decision_transitions(
        self,
    ):
        actor, reviewer = self.members["code2"], self.members["agy2"]
        self.sync("code2")
        task = self.tasks.take(actor, self.team["id"])
        self.tasks.report(
            actor, task["id"], task["revision"], "First result", "Evidence"
        )
        self.sync("agy2")
        self.tasks.review(
            reviewer, task["id"], task["revision"] + 1, "revise", "Correct source"
        )
        self.sync("code2")
        claimed = self.tasks.take(actor, self.team["id"])
        self.tasks.report(
            actor, task["id"], claimed["revision"], "Corrected", "Verified source"
        )
        self.sync("agy2")
        self.tasks.review(
            reviewer, task["id"], claimed["revision"] + 1, "accept", "Checked source"
        )
        self.sync("code2")
        decisions = PeerDecisions(self.peers)
        decisions.respond(actor, self.decision["id"], 1, "object", "Missing boundary")
        decisions.respond(
            actor, self.decision["id"], 2, "agree", "Boundary established"
        )
        before = self.dump()
        detail = team_detail(self.store.state, self.team["id"])
        workflow = detail["workflow"]
        self.assertEqual(workflow["task_ids"], [task["id"]])
        history = [e for e in workflow["events"] if e["target_type"] == "task"]
        self.assertEqual(
            [e["state"] for e in history],
            ["ready", "working", "review", "ready", "working", "review", "accepted"],
        )
        self.assertEqual(history[-1]["actor"], "agy2")
        self.assertEqual(history[-1]["round"], 1)
        decision_events = [
            e for e in workflow["events"] if e["target_type"] == "decision"
        ]
        self.assertEqual(
            [e["state"] for e in decision_events], ["proposed", "disputed", "agreed"]
        )
        self.assertEqual(detail["tasks"]["items"][0]["requester"], "agy1")
        self.assertFalse(workflow["truncated"])
        self.assertEqual(self.dump(), before)

    def test_workflow_bounds_history_and_reports_unreadable_snapshot_without_guessing(
        self,
    ):
        with self.store.transaction() as db:
            db.execute(
                "UPDATE peer_task_history SET snapshot='not JSON' WHERE task_id=?",
                (self.task["id"],),
            )
        result = team_detail(self.store.state, self.team["id"])["workflow"]
        self.assertEqual(result["invalid_records"], 1)
        event = next(e for e in result["events"] if e["target_type"] == "task")
        self.assertIsNone(event["state"])
        with self.store.transaction() as db:
            db.executemany(
                "INSERT INTO peer_task_history(task_id,revision,actor,kind,snapshot,created) VALUES(?,?,?,?,?,?)",
                [
                    (
                        self.task["id"],
                        i,
                        self.members["code2"],
                        "fixture",
                        json.dumps({"state": "working", "rounds": 0}),
                        1795500000 + i,
                    )
                    for i in range(2, 208)
                ],
            )
        before = self.dump()
        result = team_detail(self.store.state, self.team["id"])["workflow"]
        self.assertLessEqual(len(result["events"]), 200)
        self.assertTrue(result["truncated"])
        self.assertEqual(self.dump(), before)

    def test_workflow_message_flows_use_team_deliveries_and_metadata_only(self):
        team = self.team["id"]
        direct = self.peers.message(
            self.members["agy1"], team, "direct", "Private <b>body</b>", "wf-direct", ["code2"]
        )
        handoff = self.peers.message(
            self.members["code2"], team, "handoff", "Handoff body", "wf-handoff", ["agy1", "agy2"]
        )
        blocker = self.peers.message(self.members["agy2"], team, "blocker", "Blocker body", "wf-blocker")
        self.peers.message(self.members["agy1"], team, "milestone", "Milestone body", "wf-milestone")
        self.sync("code2")
        before = self.dump()
        workflow = team_detail(self.store.state, team)["workflow"]
        self.assertEqual(self.dump(), before)
        by_seq = {m["seq"]: m for m in workflow["messages"]}
        self.assertEqual(set(by_seq), {direct["seq"], handoff["seq"], blocker["seq"]})
        self.assertNotIn("milestone", {m["kind"] for m in workflow["messages"]})
        self.assertEqual(by_seq[direct["seq"]]["recipients"], [{"alias": "code2", "received": True}])
        self.assertEqual(
            by_seq[handoff["seq"]]["recipients"],
            [{"alias": "agy1", "received": False}, {"alias": "agy2", "received": False}],
        )
        self.assertTrue(by_seq[direct["seq"]]["edge"] and by_seq[handoff["seq"]]["edge"])
        self.assertFalse(by_seq[blocker["seq"]]["edge"])
        events = {e["seq"]: e for e in workflow["events"] if e["target_type"] == "message"}
        self.assertEqual(set(events), set(by_seq))
        self.assertEqual(events[direct["seq"]]["state"], "received")
        self.assertEqual(events[handoff["seq"]]["state"], "sent")
        self.assertEqual(events[direct["seq"]]["actor"], "agy1")
        for record in [*workflow["messages"], *events.values()]:
            self.assertNotIn("body", record)
        self.assertNotIn("Private", json.dumps(workflow))
        self.assertEqual(workflow["limits"]["messages"], 100)

    def test_workflow_messages_are_bounded_and_older_ones_resolve_by_cursor(self):
        team = self.team["id"]
        sent = [
            self.peers.message(
                self.members["agy1"], team, "direct", f"Note {i}", f"bulk-{i}", ["code2"]
            )
            for i in range(130)
        ]
        detail = team_detail(self.store.state, team)
        workflow = detail["workflow"]
        self.assertEqual(len(workflow["messages"]), 100)
        self.assertTrue(workflow["truncated"])
        self.assertEqual(
            [m["seq"] for m in workflow["messages"]], [m["seq"] for m in sent[-100:]]
        )
        oldest = workflow["messages"][0]
        self.assertNotIn(oldest["id"], {row["id"] for row in detail["activity"]["items"]})
        page = record_page(self.store.state, team, "activity", json.dumps(oldest["seq"] + 1))
        self.assertEqual(page["items"][0]["id"], oldest["id"])
        self.assertEqual(page["items"][0]["body"], "Note 30")

    def test_monitor_summary_reports_unresponsive_and_degraded_from_heartbeat(self):
        import time
        from native_team import monitor

        beat = self.store.state / monitor.HEARTBEAT

        def write(updated, backoff):
            beat.write_text(json.dumps({"version": 1, "instance": "0" * 32, "started_at": updated, "updated": updated, "backoff": backoff, "scan_retries": 0}))

        entry = {"member": "m", "restarts": 1, "due_in": 5, "error": "OperationalError"}
        # Real cross-process lock probing is covered in test_monitor; here the companion is held.
        with patch.object(monitor, "running", return_value=True), patch.object(monitor, "instance_held", return_value=True):
            detail = team_detail(self.store.state, self.team["id"])["monitor"]
            self.assertEqual((detail["running"], detail["status"]), (True, "running"))
            write(time.time() - 300, [])
            detail = team_detail(self.store.state, self.team["id"])["monitor"]
            self.assertEqual((detail["running"], detail["status"]), (True, "unresponsive"))
            write(time.time(), [entry])
            listing = directory(self.store.state)["monitor"]
            self.assertEqual((listing["status"], listing["backoff"]), ("degraded", 1))
        self.assertEqual(team_detail(self.store.state, self.team["id"])["monitor"]["status"], "stopped")

    def test_closed_history_omits_current_availability_and_live_wait_ages(self):
        Availability(self.store).set(
            self.closed_member, 0, "cooldown", "Unrelated current quota"
        )
        before = self.dump()
        detail = team_detail(self.store.state, self.closed["id"])
        inspected = self.peers.inspect(self.closed["project"])[0]
        for data in (detail, inspected):
            self.assertEqual(data["state"], "cancelled")
            for member in data["members"]:
                self.assertNotIn("availability", member)
                self.assertNotIn("waiting", member)
        self.assertEqual(self.dump(), before)

    def test_cooldown_and_wait_age_distinguish_unread_from_unfinished_without_writes(
        self,
    ):
        actor = self.members["code2"]
        Availability(self.store).set(actor, 0, "cooldown", "Native reset notice", 1500)
        message = self.peers.message(
            self.members["agy1"],
            self.team["id"],
            "direct",
            "Please continue",
            "wait-age",
            ["code2"],
        )
        with self.store.transaction() as db:
            db.execute(
                "UPDATE peer_messages SET created=1000 WHERE id=?", (message["id"],)
            )
            db.execute(
                "UPDATE peer_tasks SET updated=1100 WHERE id=?", (self.task["id"],)
            )
        before = self.dump()
        with patch("native_team.workbench_data.time.time", return_value=1200):
            data = team_detail(self.store.state, self.team["id"])
        member = next(m for m in data["members"] if m["alias"] == "code2")
        self.assertTrue(member["availability"]["held"])
        self.assertEqual(member["waiting"]["message_age_seconds"], 200)
        self.assertEqual(member["waiting"]["task_age_seconds"], 100)
        self.assertEqual(self.dump(), before)
        self.peers.sync(actor, self.team["id"])
        after_read = team_detail(self.store.state, self.team["id"])
        member = next(m for m in after_read["members"] if m["alias"] == "code2")
        self.assertEqual(member["waiting"]["unreceived_messages"], 0)
        self.assertEqual(member["waiting"]["pending_tasks"], 1)
        self.assertFalse(member["availability"]["held"])
        self.assertTrue(member["availability"]["resume_due"])

    def test_whole_team_attention_survives_history_pagination_and_preserves_handoffs(
        self,
    ):
        with self.store.transaction() as db:
            db.execute(
                "UPDATE peer_tasks SET created=1,handoff_needed=1,reviewer_handoff_needed=1,paths='[\"src\"]' WHERE id=?",
                (self.task["id"],),
            )
            db.execute(
                "UPDATE peer_decisions SET created=1 WHERE id=?", (self.decision["id"],)
            )
            for i in range(55):
                db.execute(
                    "INSERT INTO peer_tasks(id,team_id,created_by,owner,reviewer,brief,acceptance,state,request_key,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        f"closed-{i}",
                        self.team["id"],
                        self.members["agy1"],
                        self.members["code2"],
                        self.members["agy2"],
                        "Finished history",
                        "Verified",
                        "accepted",
                        f"closed-{i}",
                        100 + i,
                        100 + i,
                    ),
                )
                db.execute(
                    "INSERT INTO peer_decisions(id,team_id,proposed_by,topic,proposal,state,request_key,created) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        f"agreed-{i}",
                        self.team["id"],
                        self.members["agy1"],
                        "Past",
                        "Kept",
                        "agreed",
                        f"agreed-{i}",
                        100 + i,
                    ),
                )
        before = self.dump()
        detail = team_detail(self.store.state, self.team["id"])
        self.assertFalse(
            any(t["id"] == self.task["id"] for t in detail["tasks"]["items"])
        )
        self.assertEqual(detail["task_counts"]["ready"], 1)
        self.assertEqual(detail["decision_counts"]["proposed"], 1)
        self.assertEqual(detail["attention"]["handoff_tasks"], 1)
        self.assertEqual(len(detail["workflow"]["task_ids"]), 30)
        self.assertEqual(len(detail["workflow"]["decision_ids"]), 20)
        self.assertNotIn(self.task["id"], detail["workflow"]["task_ids"])
        # Imported records without task history must not acquire invented transitions.
        self.assertFalse(
            any(e["target_type"] == "task" for e in detail["workflow"]["events"])
        )
        older = record_page(
            self.store.state, self.team["id"], "tasks", detail["tasks"]["next_cursor"]
        )
        row = next(t for t in older["items"] if t["id"] == self.task["id"])
        self.assertEqual(
            (row["handoff_needed"], row["reviewer_handoff_needed"], row["paths"]),
            (1, 1, ["src"]),
        )
        self.assertEqual(self.dump(), before)

    def test_delivery_attention_names_recipients_without_exposing_global_contact(self):
        message = self.peers.message(
            self.members["agy1"],
            self.team["id"],
            "blocker",
            "Needs attention",
            "failure",
            ["code2"],
        )
        with self.store.transaction() as db:
            db.execute(
                "UPDATE peer_deliveries SET state='uncertain' WHERE message_id=?",
                (message["id"],),
            )
        detail = team_detail(self.store.state, self.team["id"])
        self.assertEqual(detail["attention"]["deliveries"], 1)
        row = next(m for m in detail["activity"]["items"] if m["id"] == message["id"])
        self.assertEqual(row["delivery_attention"], 1)
        self.assertEqual(row["deliveries"][0]["alias"], "code2")
        self.assertEqual(row["deliveries"][0]["state"], "uncertain")
        self.assertNotIn("last_seen", json.dumps(detail))

    def test_real_fields_cross_project_history_and_no_mutation_or_native_ids(self):
        before = self.dump()
        listing = directory(self.store.state)
        self.assertEqual(
            {row["state"] for row in listing["teams"]}, {"active", "cancelled"}
        )
        detail = team_detail(self.store.state, self.team["id"])
        code2 = next(row for row in detail["members"] if row["alias"] == "code2")
        self.assertEqual(code2["current"], "Current report")
        self.assertEqual(code2["next_step"], "Next action")
        no_board = next(row for row in detail["members"] if row["alias"] == "agy2")
        self.assertIsNone(no_board["current"])
        self.assertEqual(detail["tasks"]["items"][0]["reviewer"], "agy2")
        self.assertEqual(
            detail["decisions"]["items"][0]["responses"][0]["response"], None
        )
        self.assertNotIn("native_id", json.dumps(detail))
        with closing(self.store.connect()) as db:
            for row in db.execute(
                "SELECT native_id FROM members WHERE native_id IS NOT NULL"
            ):
                self.assertNotIn(row[0], json.dumps(detail))
        self.assertEqual(self.dump(), before)

    def test_history_is_paginated_without_missing_or_duplicate_messages(self):
        with self.store.transaction() as db:
            for i in range(115):
                db.execute(
                    "INSERT INTO peer_messages(id,team_id,sender,kind,body,request_key,created) VALUES(?,?,?,?,?,?,?)",
                    (
                        f"history-{i}",
                        self.team["id"],
                        self.members["agy1"],
                        "milestone",
                        f"Record {i}",
                        f"history-{i}",
                        i,
                    ),
                )
        before = self.dump()
        page = team_detail(self.store.state, self.team["id"])["activity"]
        ids = [row["id"] for row in page["items"]]
        total = page["total"]
        self.assertEqual(len(ids), 50)
        while page["next_cursor"] is not None:
            page = record_page(
                self.store.state, self.team["id"], "activity", page["next_cursor"]
            )
            ids.extend(row["id"] for row in page["items"])
        self.assertEqual(len(ids), total)
        self.assertEqual(len(set(ids)), total)
        self.assertEqual(self.dump(), before)

    def test_missing_legacy_and_invalid_inputs_do_not_create_or_migrate_state(self):
        missing = self.root / "missing"
        self.assertEqual(directory(missing)["teams"], [])
        self.assertFalse(missing.exists())
        legacy = Store(self.root / "legacy")
        with self.assertRaises(TeamError):
            directory(legacy.state)
        with closing(sqlite3.connect(legacy.path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 0)
        with self.assertRaises(LookupError):
            team_detail(self.store.state, "not-a-team")
        for section, cursor in [
            ("members", None),
            ("activity", "garbage"),
            ("tasks", '["bad",0]'),
        ]:
            with self.assertRaises(ValueError):
                record_page(self.store.state, self.team["id"], section, cursor)


class WorkbenchHTTPTests(WorkbenchFixture):
    def test_loopback_startup_is_independent_of_hostname_resolution(self):
        with patch("socket.getfqdn", side_effect=AssertionError("unexpected DNS lookup")):
            with make_server(self.store.state) as server:
                self.assertEqual(server.server_name, "127.0.0.1")
                self.assertGreater(server.server_port, 0)

    def test_idle_preconnect_does_not_block_page_reads(self):
        with socket.create_connection(
            ("127.0.0.1", self.server.server_port), timeout=3
        ) as idle:
            idle.sendall(b"GET / HTTP/1.1\r\n")
            self.assertEqual(self.request("/api/teams")[0], 200)

    def setUp(self):
        super().setUp()
        self.server = make_server(self.store.state, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def request(self, path, method="GET", headers=None):
        conn = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            conn.request(method, path, headers=headers or {})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def test_api_static_assets_and_refresh_have_no_ledger_side_effects(self):
        before = self.dump()
        for _ in range(2):
            status, headers, body = self.request("/api/teams")
            self.assertEqual(status, 200)
            self.assertEqual(len(json.loads(body)["teams"]), 2)
            self.assertEqual(headers["Cache-Control"], "no-store")
            status, _, body = self.request(f"/api/teams/{self.team['id']}")
            self.assertEqual(status, 200)
            self.assertIn(b"Current report", body)
        status, _, html = self.request("/")
        self.assertEqual(status, 200)
        for name, media in (
            ("styles.css", "text/css"),
            ("mark.svg", "image/svg+xml"),
            ("app.js", "text/javascript"),
            ("i18n.js", "text/javascript"),
            ("data-source.js", "text/javascript"),
            ("workflow.js", "text/javascript"),
        ):
            with self.subTest(asset=name):
                self.assertIn(("./" + name).encode(), html)
                status, headers, body = self.request("/" + name)
                self.assertEqual(status, 200)
                self.assertEqual(headers["Content-Type"].split(";")[0], media)
                self.assertTrue(body)
        self.assertIn(b'"live"', self.request("/config.js")[2])
        self.assertEqual(self.request("/fixtures.js")[0], 404)
        self.assertEqual(self.dump(), before)

    def test_wrong_origin_host_mutations_and_path_traversal_are_rejected(self):
        before = self.dump()
        for headers in [
            {"Host": "attacker.example"},
            {"Origin": "https://attacker.example"},
            {"Sec-Fetch-Site": "cross-site"},
            {"Sec-Fetch-Site": "same-site"},
        ]:
            self.assertEqual(self.request("/api/teams", headers=headers)[0], 403)
        for method in ["POST", "PUT", "PATCH", "DELETE", "OPTIONS"]:
            self.assertEqual(self.request("/api/teams", method)[0], 405)
        for path in [
            "/../README.md",
            "/%2e%2e/README.md",
            "/team.sqlite3",
            "/api/teams/missing",
        ]:
            self.assertEqual(self.request(path)[0], 404)
        self.assertEqual(
            self.request(f"/api/teams/{self.team['id']}/activity?cursor=bad")[0], 400
        )
        self.assertEqual(self.dump(), before)

    def test_monitor_status_reports_stopped_with_pending_codex_deliveries(self):
        status, _, body = self.request("/api/teams")
        self.assertEqual(status, 200)
        directory = json.loads(body)
        self.assertIn("monitor", directory)
        self.assertFalse(directory["monitor"]["running"])
        self.assertGreaterEqual(directory["monitor"]["all_active_codex"], 1)

        status, _, body = self.request(f"/api/teams/{self.team['id']}")
        self.assertEqual(status, 200)
        detail = json.loads(body)
        self.assertIn("monitor", detail)
        self.assertFalse(detail["monitor"]["running"])
        expected_codex = sum(
            m["provider"] == "codex" and m["state"] == "active"
            for m in detail["members"]
        )
        self.assertEqual(detail["monitor"]["active_codex"], expected_codex)
        self.assertGreaterEqual(expected_codex, 1)
