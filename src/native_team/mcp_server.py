"""Project tools called by the native clients; no provider inference backend."""

import asyncio
import os
from pathlib import Path
import shlex
import time

from mcp.server.fastmcp import FastMCP
from pydantic import StrictFloat, StrictInt

from .monitor import running
from .identity import ConnectionIdentity
from .availability import Availability
from .peers import PeerStore
from .peer_tools import register
from .store import CLAUDE_ALIASES, Store, TeamError, conversation_uuid


def serve(state, provider):
    store = Store(state)
    participant = None
    claude_needs_confirmation = False
    peers = PeerStore(store)
    identity = ConnectionIdentity(store.state)
    mcp = FastMCP(
        "native-team",
        instructions=(
            "Whole-work coordination for independent native clients. Join THIS conversation first. "
            "Flat teams are peers with individual workstreams; use peer_* tools for new teams. "
            "No permanent coordinator or provider seniority. Legacy pair tools keep their original contracts. "
            "Edits require user-authorized scope; declaring a scope is not OS permission. "
            "Do not use Session Hub. After receipt act on the returned action: review, continue, "
            "checkpoint, finish or report a real blocker. Never confuse a result with accepted work. "
            "agy and Claude Code own their native background waits. Claude uses its own UUID and approved lane. "
            "Keep native permissions; no /goal command required."
        ),
    )

    def actor():
        if participant is None:
            raise TeamError("Join this native conversation first")
        if claude_needs_confirmation:
            raise TeamError(
                "Rejoin with this Claude session's current UUID before using the team"
            )
        store.touch(participant["id"])
        return participant["id"]

    def changed(value):
        return {"changed": value, "coordination": store.status(actor())}

    @mcp.tool()
    def team_join(
        alias: str, project: str, native_session_id: str | None = None
    ) -> dict:
        """Join this conversation. Codex uses CODEX_THREAD_ID. Claude uses CLAUDE_CODE_SESSION_ID or its own explicit session UUID when absent."""
        nonlocal participant, claude_needs_confirmation
        project = str(Path(project).expanduser().resolve())
        if provider == "claude":
            if claude_needs_confirmation and native_session_id is None:
                raise TeamError(
                    "Confirm the current Claude UUID explicitly or reconnect the MCP server"
                )
            # A failed rejoin after a native session switch must not leave the old identity usable.
            claude_needs_confirmation = participant is not None
        if participant and (participant["alias"], participant["project"]) != (
            alias,
            project,
        ):
            raise TeamError(
                "This native connection is already joined to another identity"
            )
        native_id = None
        if provider == "codex":
            native_id = native_session_id or os.environ.get("CODEX_THREAD_ID")
        elif provider == "claude":
            inherited = os.environ.get("CLAUDE_CODE_SESSION_ID")
            native_id = conversation_uuid(
                provider, inherited if inherited is not None else native_session_id
            )
            if (
                native_session_id is not None
                and conversation_uuid(provider, native_session_id) != native_id
            ):
                raise TeamError(
                    "The supplied session UUID differs from this Claude Code session"
                )
        if provider == "agy" or (provider == "claude" and alias in CLAUDE_ALIASES):
            identity.acquire(provider, project, alias)
        try:
            participant = store.join(provider, alias, project, native_id)
            claude_needs_confirmation = False
        except Exception:
            if participant is None:
                identity.close()
            raise
        result = {
            "participant": participant,
            "coordination": store.status(participant["id"], include_closed=False),
        }
        if provider == "codex":
            result["notifications"] = (
                "persistent native Codex monitor active"
                if running(store.state)
                else "start the native-team-monitor user service"
            )
        elif provider == "claude":
            result["notifications"] = (
                "Read team_state, then peer_sync and peer_ack. When idle, run wait_command "
                "with Bash in the background in THIS session. Exit 0: sync, act, then re-arm. "
                "124: idle deadline. 1: inspect the error before retrying."
            )
            result["wait_command"] = shlex.join(
                [
                    "native-team",
                    "--state",
                    str(store.state),
                    "wait",
                    "--project",
                    project,
                    "--alias",
                    alias,
                    "--mode",
                    "actionable",
                    "--coalesce",
                    "3",
                ]
            )
        else:
            from .startup import recovery_guidance

            result["notifications"] = (
                "native-owned event wait or authorized schedule; verify this conversation's actual continuation"
            )
            result["recovery_setup"] = recovery_guidance(project, alias, store.state)
        return result

    @mcp.tool()
    def team_state(include_closed: bool = False) -> dict:
        """Receive active legacy notices and inspect current teams. Request include_closed for historical or parked legacy details and outstanding closed legacy notices. Sync and acknowledge pending_closed_team_ids with peer_sync and peer_ack."""
        current = actor()
        inspect = store.receive if provider == "codex" else store.status
        result = inspect(current, include_closed=include_closed)
        result.update(peers.overview(current, include_closed=include_closed))
        result["availability"] = Availability(store).get(current)
        return result

    @mcp.tool()
    def team_availability(
        status: str | None = None,
        revision: int | None = None,
        reason: str = "",
        resume_after: str | StrictInt | StrictFloat | None = None,
    ) -> dict:
        """Read or record THIS conversation's quota cooldown; never infer quota or mutate a peer. Get revision before changing. None reset means unknown, not automatic recovery."""
        service, current = Availability(store), actor()
        if status is None:
            if revision is not None or reason or resume_after is not None:
                raise TeamError(
                    "Read availability without write parameters, or specify a status"
                )
            return service.get(current)
        return service.set(current, revision, status, reason, resume_after)

    @mcp.tool()
    def team_quota(
        posture: str | None = None,
        revision: int | None = None,
        reason: str = "",
        resume_after: str | StrictInt | StrictFloat | None = None,
        observations: list[dict] | None = None,
    ) -> dict:
        """Read/record THIS member's unknown/normal/conserve/cooldown posture. No provider queries. Snapshot fields: source, scope(account/model/window), non-secret bucket, observed_at, expires_at, optional remaining+unit and resets_at. Timestamps need timezone or epoch. Omit observations to preserve, [] to clear. Observations never schedule recovery or represent reserved/shared capacity. Read revision first; reload native MCP to discover this tool."""
        service, current = Availability(store), actor()
        if posture is None:
            if (
                revision is not None
                or reason
                or resume_after is not None
                or observations is not None
            ):
                raise TeamError(
                    "Read quota without write parameters, or specify a posture"
                )
            return service.get(current)
        return service.record(
            current, revision, posture, reason, resume_after, observations=observations
        )

    @mcp.tool()
    def team_work(
        intent: str, acceptance: str, next_step: str, request_key: str
    ) -> dict:
        """Coordinator records authorized work emerging from normal conversation; not native /goal."""
        return changed(store.start(actor(), intent, acceptance, next_step, request_key))

    @mcp.tool()
    def team_assign(
        work_id: str, owner: str, brief: str, acceptance: str, request_key: str
    ) -> dict:
        """Assign bounded work to an explicitly enrolled native agy adviser. Stable keys deduplicate."""
        return changed(
            store.assign(actor(), work_id, owner, brief, acceptance, request_key)
        )

    @mcp.tool()
    async def team_take(wait_seconds: int = 0) -> dict:
        """Native agy atomically takes/resumes its next assignment; wait is bounded to 45 seconds."""
        if type(wait_seconds) is not int or not 0 <= wait_seconds <= 45:
            raise TeamError("Wait must be from 0 to 45 seconds")
        current, deadline = actor(), time.monotonic() + wait_seconds
        while True:
            task = await asyncio.to_thread(store.take, current)
            if task or time.monotonic() >= deadline:
                return {"assignment": task, "coordination": store.status(current)}
            await asyncio.sleep(min(0.5, max(0, deadline - time.monotonic())))

    @mcp.tool()
    def team_report(
        assignment_id: str,
        revision: int,
        summary: str,
        evidence: str,
        blocked: bool = False,
    ) -> dict:
        """Assigned owner submits evidence for review, or an exact blocker; never accepts its own work."""
        return changed(
            store.report(actor(), assignment_id, revision, summary, evidence, blocked)
        )

    @mcp.tool()
    def team_review(
        assignment_id: str, revision: int, decision: str, explanation: str
    ) -> dict:
        """Coordinator accepts, revises (max two rounds), or blocks. Continue the returned next action."""
        return changed(
            store.decide(actor(), assignment_id, revision, decision, explanation)
        )

    @mcp.tool()
    def team_update(
        work_id: str, revision: int, state: str, next_step: str, evidence: str = ""
    ) -> dict:
        """Checkpoint active work, pause/block/cancel, or finish with integrated evidence after review."""
        return changed(
            store.update_work(actor(), work_id, revision, state, next_step, evidence)
        )

    register(mcp, peers, actor)
    try:
        mcp.run(transport="stdio")
    finally:
        identity.close()
