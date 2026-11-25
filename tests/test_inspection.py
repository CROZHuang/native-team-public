from contextlib import closing
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from native_team.peers import PeerStore
from native_team.schema import migrate
from native_team.store import Store


class TeamListingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.store = Store(self.root / "state")
        migrate(self.store)
        self.peers = PeerStore(self.store)
        self.projects = [self.root / "Game Project", self.root / "Tool Project"]
        for project, name, alias in zip(
            self.projects,
            ["game-team", "Completed acceptance"],
            ["agy-game", "agy-test"],
        ):
            project.mkdir()
            member = self.store.join("agy", alias, project)
            self.store.join("agy", alias + "-peer", project)
            team = self.peers.create(
                member["id"], name, "Private purpose", [alias, alias + "-peer"], name
            )
            if alias == "agy-test":
                self.peers.set_state(
                    member["id"],
                    team["id"],
                    1,
                    "done",
                    "Fixture completion",
                    "Verified",
                )
        self.store.join("agy", "unassigned-old-window", self.projects[0])

    def run_cli(self, *args, state=None):
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "native_team.cli",
                "--state",
                str(state or self.store.state),
                "teams",
                *args,
            ],
            text=True,
            capture_output=True,
            timeout=10,
        )

    def test_lists_all_projects_and_closed_teams_without_consuming_context(self):
        with closing(self.store.connect()) as db:
            before = list(db.iterdump())
        result = self.run_cli("--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        teams = json.loads(result.stdout)
        self.assertEqual(
            {t["name"] for t in teams}, {"game-team", "Completed acceptance"}
        )
        self.assertEqual({t["state"] for t in teams}, {"active", "done"})
        self.assertEqual({t["project"] for t in teams}, {str(p) for p in self.projects})
        self.assertNotIn("Private purpose", result.stdout)
        self.assertNotIn("unassigned-old-window", result.stdout)
        self.assertNotIn("native_id", result.stdout)
        with closing(self.store.connect()) as db:
            self.assertEqual(list(db.iterdump()), before)

    def test_project_filter_and_readable_output(self):
        result = self.run_cli("--project", str(self.projects[0]))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("game-team", result.stdout)
        self.assertIn("agy-game", result.stdout)
        self.assertIn(str(self.projects[0]), result.stdout)
        self.assertNotIn("Completed acceptance", result.stdout)
        self.assertNotIn("last native tool contact", result.stdout)

    def test_missing_state_is_not_created(self):
        state = self.root / "not-installed"
        result = self.run_cli("--json", state=state)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [])
        self.assertFalse(state.exists())

    def test_legacy_ledger_is_not_migrated_by_listing(self):
        legacy = Store(self.root / "legacy")
        result = self.run_cli("--json", state=legacy.state)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [])
        with closing(legacy.connect()) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 0)
