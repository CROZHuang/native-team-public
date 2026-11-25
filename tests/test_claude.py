from contextlib import AsyncExitStack, asynccontextmanager, closing
import asyncio
import json
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client

from native_team.availability import Availability
from native_team.outbox import Outbox
from native_team.peers import PeerStore
from native_team.schema import migrate
from native_team.store import Store, TeamError
from native_team.waiting import pending


LANES = ("claude-rules", "claude-history", "claude-art", "claude-ai-test")


def pin_directory(pin, target):
    try:
        pin.symlink_to(target, target_is_directory=True)
    except OSError as error:
        if getattr(error, "winerror", None) == 1314:
            raise unittest.SkipTest(
                "Project pins need Windows directory symlink permission"
            ) from error
        raise


class ClaudeIdentityTests(unittest.TestCase):
    def test_project_bound_lane_is_exact_and_cannot_share_another_uuid(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = Store(root / "state")
            pathology = root / "test project" / "研究"
            child = pathology / "child"
            child.mkdir(parents=True)
            pin = store.state / "claude-pathology-project"
            alias = "claude-pathology"
            native_id, other_id = str(uuid.uuid4()), str(uuid.uuid4())

            with self.assertRaises(TeamError):
                store.join("claude", alias, pathology, native_id)
            pin_directory(pin, pathology)
            for other in (root, pathology.parent, child):
                with self.subTest(other=other), self.assertRaises(TeamError):
                    store.join("claude", alias, other, native_id)
            for lane in LANES:
                with self.subTest(lane=lane), self.assertRaises(TeamError):
                    store.join("claude", lane, pathology, other_id)
            store.join("claude", LANES[0], root, other_id)
            with self.assertRaises(TeamError):
                store.join("claude", alias, pathology, other_id)
            with self.assertRaises(TeamError):
                store.join("codex", alias, pathology, native_id)

            first = store.join("claude", alias, pathology, native_id)
            alternate = root / "same directory"
            alternate.symlink_to(pathology, target_is_directory=True)
            self.assertEqual(
                store.join("claude", alias, alternate, native_id)["id"], first["id"]
            )
            with self.assertRaises(TeamError):
                store.join("claude", alias, pathology, str(uuid.uuid4()))
            with self.assertRaises(TeamError):
                store.join("claude", LANES[1], root, native_id)
            pin.unlink()
            pin.symlink_to(child, target_is_directory=True)
            with self.assertRaises(TeamError):
                store.join("claude", alias, child, str(uuid.uuid4()))
            child.rmdir()  # A broken installation pin must fail closed.
            with self.assertRaises(TeamError):
                store.join("claude", alias, pathology, native_id)
            with closing(store.connect()) as db:
                self.assertEqual(
                    db.execute("SELECT count(*) FROM members").fetchone()[0], 2
                )

    def test_reserved_lanes_and_stable_native_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = Store(root / "state")
            native_id = str(uuid.uuid4())
            original = store.join("claude", LANES[0], root, native_id)
            self.assertEqual(
                store.join("claude", LANES[0], root, native_id.upper())["id"],
                original["id"],
            )
            self.assertNotIn("native_id", original)
            for alias, identity in [
                (LANES[1], native_id),
                (LANES[0], str(uuid.uuid4())),
                ("another-lane", native_id),
                (LANES[1], None),
                (LANES[1], "invalid"),
            ]:
                with self.subTest(alias=alias), self.assertRaises(TeamError):
                    store.join("claude", alias, root, identity)
            with self.assertRaises(TeamError):
                store.join("agy", LANES[3], root)
            self.assertNotEqual(
                store.join("codex", "codex-test", root, native_id)["id"], original["id"]
            )

    def test_held_wait_keeps_work_and_rejects_the_wrong_session(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            store = Store(root / "state")
            migrate(store)
            session_id = str(uuid.uuid4())
            alias = LANES[0]
            actor = store.join("claude", alias, root, session_id)["id"]
            other = store.join("agy", "agy-test", root)["id"]
            PeerStore(store).create(
                other, "Work", "Test activation", ["agy-test", alias], "activation"
            )
            args = (store.path, str(root), alias)
            with patch(
                "native_team.waiting.os.environ", {"CLAUDE_CODE_SESSION_ID": session_id}
            ):
                availability = Availability(store)
                availability.set(actor, 0, "cooldown", "No reset time available")
                self.assertEqual(pending(*args, "actionable"), [])
                availability.set(actor, 1, "ready", "Native client resumed")
                self.assertIn("team_activation", pending(*args, "actionable"))
                with self.assertRaises(TeamError):
                    pending(*args, "recovery")
            with patch(
                "native_team.waiting.os.environ",
                {"CLAUDE_CODE_SESSION_ID": str(uuid.uuid4())},
            ):
                with self.assertRaises(TeamError):
                    pending(*args, "actionable")


@asynccontextmanager
async def client(state, provider, env=None):
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "native_team.cli",
            "--state",
            str(state),
            "mcp",
            "--client",
            provider,
        ],
        env=env or {},
    )
    async with (
        stdio_client(params) as (reader, writer),
        ClientSession(reader, writer) as session,
    ):
        await asyncio.wait_for(session.initialize(), 10)
        yield session


async def tool(session, tool_name, **args):
    result = await asyncio.wait_for(session.call_tool(tool_name, args), 10)
    if result.isError:
        raise AssertionError(str(result.content))
    return result.structuredContent or json.loads(result.content[0].text)


class ClaudeTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_pathology_native_resume_and_idle_wait_without_a_team(self):
        with tempfile.TemporaryDirectory(prefix="native team '") as folder:
            root = Path(folder).resolve()
            store = Store(root / "state")
            migrate(store)
            project = root / "test project" / "研究"
            project.mkdir(parents=True)
            pin_directory(store.state / "claude-pathology-project", project)
            args = {"alias": "claude-pathology", "project": str(project)}
            env = {"CLAUDE_CODE_SESSION_ID": str(uuid.uuid4())}
            async with client(store.state, "claude", env) as first:
                self.assertTrue(
                    (
                        await first.call_tool(
                            "team_join", dict(args, project=str(root))
                        )
                    ).isError
                )
                joined = await tool(first, "team_join", **args)
            async with client(store.state, "claude", env) as resumed:
                again = await tool(resumed, "team_join", **args)
                self.assertEqual(
                    joined["participant"]["id"], again["participant"]["id"]
                )
                self.assertEqual((await tool(resumed, "team_state"))["teams"], [])
                command, *wait = shlex.split(again["wait_command"])
                self.assertEqual(command, "native-team")
                self.assertEqual(
                    wait[-8:],
                    [
                        "--project",
                        str(project),
                        "--alias",
                        args["alias"],
                        "--mode",
                        "actionable",
                        "--coalesce",
                        "3",
                    ],
                )
                proc = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "native_team.cli",
                    *wait,
                    "--timeout",
                    "1",
                    env={**get_default_environment(), **env},
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                out, err = await asyncio.wait_for(proc.communicate(), 10)
                self.assertEqual(proc.returncode, 124, err)
                self.assertEqual(
                    json.loads(out), {"ready": False, "reason": "deadline"}
                )

    async def test_four_lanes_queue_wake_resume_and_ack_in_isolation(self):
        with tempfile.TemporaryDirectory(prefix="native team '") as folder:
            root = Path(folder).resolve()
            store = Store(root / "state")
            migrate(store)
            join = {"project": str(root)}
            ids = {alias: str(uuid.uuid4()) for alias in LANES}
            wait_commands = {}

            async def run_wait(alias, *extra, timeout=2):
                executable, *args = shlex.split(wait_commands[alias])
                self.assertEqual(executable, "native-team")
                proc = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "native_team.cli",
                    *args,
                    "--timeout",
                    str(timeout),
                    *extra,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env={
                        **get_default_environment(),
                        "CLAUDE_CODE_SESSION_ID": ids[alias],
                    },
                )
                out, err = await asyncio.wait_for(proc.communicate(), 12)
                return proc.returncode, out, err

            async with AsyncExitStack() as group:
                sender = await group.enter_async_context(client(store.state, "codex"))
                codex = await tool(
                    sender,
                    "team_join",
                    alias="codex-test",
                    native_session_id=str(uuid.uuid4()),
                    **join,
                )
                sessions = {}
                for alias in LANES[1:]:
                    session = await group.enter_async_context(
                        client(
                            store.state,
                            "claude",
                            {"CLAUDE_CODE_SESSION_ID": ids[alias]},
                        )
                    )
                    mismatch = await session.call_tool(
                        "team_join",
                        dict(join, alias=alias, native_session_id=str(uuid.uuid4())),
                    )
                    self.assertTrue(mismatch.isError)
                    bound = await tool(session, "team_join", alias=alias, **join)
                    self.assertEqual(bound["participant"]["provider"], "claude")
                    wait_commands[alias] = bound["wait_command"]
                    self.assertNotIn(ids[alias], str(bound))
                    sessions[alias] = session

                # An in-place native switch can leave MCP holding its spawn-time UUID.
                art = sessions["claude-art"]
                attempt = dict(
                    join, alias="claude-art", native_session_id=str(uuid.uuid4())
                )
                self.assertTrue((await art.call_tool("team_join", attempt)).isError)
                self.assertTrue((await art.call_tool("team_state", {})).isError)
                self.assertTrue(
                    (
                        await art.call_tool("team_join", dict(join, alias="claude-art"))
                    ).isError
                )
                await tool(
                    art,
                    "team_join",
                    alias="claude-art",
                    native_session_id=ids["claude-art"],
                    **join,
                )

                target = LANES[0]
                args = dict(join, alias=target, native_session_id=ids[target])
                # A Codex parent must never establish the identity of a Claude connection.
                async with client(
                    store.state, "claude", {"CODEX_THREAD_ID": ids[target]}
                ) as first:
                    self.assertTrue(
                        (
                            await first.call_tool("team_join", dict(join, alias=target))
                        ).isError
                    )
                    original = await tool(first, "team_join", **args)
                    wait_commands[target] = original["wait_command"]
                    self.assertEqual(
                        shlex.split(wait_commands[target])[-4:],
                        ["--mode", "actionable", "--coalesce", "3"],
                    )
                    async with client(store.state, "claude") as duplicate:
                        self.assertTrue(
                            (await duplicate.call_tool("team_join", args)).isError
                        )
                    team = await tool(
                        sender,
                        "peer_team",
                        name="Four Claude lanes",
                        purpose="Native transport test",
                        aliases=["codex-test", *LANES],
                        request_key="four",
                    )
                    team_id = team["id"]

                    async def sync_ack(session):
                        page = await tool(session, "peer_sync", team_id=team_id)
                        await tool(
                            session,
                            "peer_ack",
                            team_id=team_id,
                            epoch=page["team"]["epoch"],
                            through_seq=page["cursor"],
                        )
                        return page

                    for session in [sender, first, *sessions.values()]:
                        await sync_ack(session)

                # The target MCP process is closed when its colleague sends this message.
                message = await tool(
                    sessions["claude-art"],
                    "peer_message",
                    team_id=team_id,
                    kind="direct",
                    body="Review the rule change",
                    request_key="rules-only",
                    recipients=[target],
                )
                self.assertEqual(
                    Outbox(store).recipients(), {codex["participant"]["id"]}
                )
                ready, idle, invalid = await asyncio.gather(
                    run_wait(target, "--coalesce", "0.1"),
                    run_wait("claude-history"),
                    run_wait(target, "--alias", "missing"),
                )
                self.assertEqual(ready[0], 0, ready[2])
                self.assertTrue(json.loads(ready[1])["ready"])
                self.assertLess(len(ready[1]), 100)
                self.assertEqual(idle[0], 124, idle[2])
                self.assertEqual(json.loads(idle[1])["reason"], "deadline")
                self.assertEqual(invalid[0], 1)

                async def receipt():
                    record = await tool(
                        sender,
                        "peer_receipts",
                        team_id=team_id,
                        message_id=message["id"],
                    )
                    self.assertEqual(
                        [r["alias"] for r in record["recipients"]], [target]
                    )
                    return record["recipients"][0]

                self.assertIsNone((await receipt())["received_at"])
                self.assertFalse(
                    (await sync_ack(sessions["claude-history"]))["messages"]
                )
                async with client(
                    store.state, "claude", {"CLAUDE_CODE_SESSION_ID": ids[target]}
                ) as resumed:
                    bound = await tool(resumed, "team_join", alias=target, **join)
                    self.assertEqual(
                        bound["participant"]["id"], original["participant"]["id"]
                    )
                    await tool(resumed, "team_state")
                    self.assertIsNone((await receipt())["received_at"])
                    page = await tool(resumed, "peer_sync", team_id=team_id)
                    self.assertEqual(
                        [m["body"] for m in page["messages"]],
                        ["Review the rule change"],
                    )
                    self.assertIsNotNone((await receipt())["received_at"])
                    self.assertIsNone((await receipt())["acked_at"])
                    await tool(
                        resumed,
                        "peer_ack",
                        team_id=team_id,
                        epoch=page["team"]["epoch"],
                        through_seq=page["cursor"],
                    )
                    self.assertIsNotNone((await receipt())["acked_at"])
                    self.assertFalse((await sync_ack(resumed))["messages"])

                    # A wait armed before a task arrives wakes the responsible peer.
                    async with asyncio.TaskGroup() as running:
                        wake = running.create_task(run_wait(target, timeout=10))
                        with self.assertRaises(TimeoutError):
                            await asyncio.wait_for(asyncio.shield(wake), 0.3)
                        task = await tool(
                            sender,
                            "peer_task_create",
                            team_id=team_id,
                            owner=target,
                            reviewer="claude-history",
                            brief="Review the fixture",
                            acceptance="Return a receipt",
                            request_key="review-fixture",
                        )
                    self.assertEqual(wake.result()[0], 0, wake.result()[2])
                    await sync_ack(resumed)
                    claim = (await tool(resumed, "peer_task_take", team_id=team_id))[
                        "task"
                    ]
                    self.assertEqual(claim["id"], task["id"])
                    report = await tool(
                        resumed,
                        "peer_task_report",
                        task_id=claim["id"],
                        revision=claim["revision"],
                        result="Fixture checked",
                        evidence="Directed delivery acknowledged",
                    )
                    reviewer = sessions["claude-history"]
                    await sync_ack(reviewer)
                    accepted = await tool(
                        reviewer,
                        "peer_task_review",
                        task_id=claim["id"],
                        revision=report["revision"],
                        decision="accept",
                        explanation="Verified fixture receipt",
                    )
                    self.assertEqual(accepted["state"], "accepted")
