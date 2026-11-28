import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from native_team.workbench_quota import (
    WorkbenchQuota,
    capture_statusline,
    read_codex_windows,
    statusline_stream,
)


class WorkbenchQuotaTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name)
        self.now = time.time()

    def test_stream_preserves_renderer_bytes_and_records_only_quota(self):
        payload = {
            "conversation_id": "private-session",
            "email": "private@example.test",
            "model": {"id": "gemini-neon"},
            "private": "面向用户的对话",
            "quota": {
                "gemini-weekly": {"remaining_fraction": 0.42},
                "3p-weekly": {"remaining_fraction": 0},
            },
        }
        raw = json.dumps(payload, ensure_ascii=False).encode()
        output = self.state / "renderer.bin"
        cmd = [
            sys.executable,
            "-c",
            "import sys,pathlib;pathlib.Path(sys.argv[1]).write_bytes(sys.stdin.buffer.read())",
            str(output),
        ]
        self.assertEqual(statusline_stream(io.BytesIO(raw), self.state, "agy", cmd), 0)
        self.assertEqual(output.read_bytes(), raw)
        api = WorkbenchQuota(self.state)
        agy = api.read()["providers"]["agy"]
        self.assertEqual(
            [(w["remaining"], w["applies"]) for w in agy["windows"]],
            [(0, False), (42, True)],
        )
        snapshot = (self.state / "quota/workbench-agy.json").read_text(encoding="utf-8")
        for sensitive in ("private-session", "private@example.test", "面向用户的对话"):
            self.assertNotIn(sensitive, snapshot)
            self.assertNotIn(sensitive, json.dumps(agy, ensure_ascii=False))
        for invalid in (b"not json", b"x" * 180000):
            self.assertEqual(
                statusline_stream(io.BytesIO(invalid), self.state, "agy", cmd), 0
            )
            self.assertEqual(output.read_bytes(), invalid)

    def test_claude_bucket_freshness_survives_repaints_and_session_switches(self):
        data = {
            "session_id": "a",
            "rate_limits": {
                "five_hour": {"used_percentage": 15},
                "seven_day": {"used_percentage": 100},
            },
        }
        capture_statusline(self.state, "claude", data, self.now - 400)
        capture_statusline(
            self.state, "claude", {**data, "session_id": "b"}, self.now - 90
        )
        data["rate_limits"]["five_hour"]["used_percentage"] = 10
        capture_statusline(self.state, "claude", data, self.now - 3)
        capture_statusline(
            self.state, "claude", {**data, "rate_limits": {}}, self.now - 50
        )
        five, week = WorkbenchQuota(self.state).read()["providers"]["claude"]["windows"]
        self.assertEqual((five["remaining"], five["stale"]), (90, False))
        self.assertEqual(
            (week["remaining"], week["stale"], week["observed_at"]),
            (0, True, self.now - 400),
        )

    def test_refresh_coalesces_codex_and_labels_previous_values_on_failure(self):
        read = Mock(
            side_effect=[[{"bucket": "codex/primary", "remaining": 0}], TimeoutError()]
        )
        quota = WorkbenchQuota(self.state, read)
        self.assertEqual(quota.read()["providers"]["codex"]["status"], "idle")
        with patch("native_team.workbench_quota.time.monotonic", return_value=100):
            self.assertEqual(
                quota.read(True)["providers"]["codex"]["windows"][0]["remaining"], 0
            )
            quota.read(True)
        self.assertEqual(read.call_count, 1)
        with patch("native_team.workbench_quota.time.monotonic", return_value=116):
            result = quota.read(True)["providers"]["codex"]
        self.assertEqual(result["status"], "unavailable")
        self.assertTrue(result["windows"][0]["stale"])

    def test_stdio_uses_only_metadata_requests_and_exits_on_timeout(self):
        fake = self.state / "cli.py"
        fake.write_text(
            "import json,sys\n"
            "def read(): return json.loads(sys.stdin.readline())\n"
            "def reply(i, result): print(json.dumps({'id':i,'result':result}), flush=True)\n"
            "assert read()['method']=='initialize'\nreply(1,{})\n"
            "assert read()['method']=='initialized'\n"
            "assert read()['method']=='account/rateLimits/read'\n"
            "reply(2,{'rateLimits':{'primary':{'usedPercent':12}}})\n",
            encoding="utf-8",
        )
        windows = read_codex_windows([sys.executable, str(fake)])
        self.assertEqual(windows[0]["remaining"], 88)
        with self.assertRaises(TimeoutError):
            read_codex_windows(
                [sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.2
            )

    def test_operator_can_use_cli_without_a_renderer_or_team(self):
        cmd = [
            sys.executable,
            "-m",
            "native_team.cli",
            "--state",
            str(self.state),
            "workbench-quota-stream",
            "claude",
        ]
        payload = {
            "session_id": "example",
            "rate_limits": {"five_hour": {"used_percentage": 0}},
        }
        result = subprocess.run(
            cmd,
            input=json.dumps(payload).encode(),
            capture_output=True,
            check=True,
            timeout=10,
        )
        self.assertEqual(result.stdout, b"")
        self.assertEqual(
            WorkbenchQuota(self.state).read()["providers"]["claude"]["windows"][0][
                "remaining"
            ],
            100,
        )
        self.assertFalse((self.state / "team.sqlite3").exists())
