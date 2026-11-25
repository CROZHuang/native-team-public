"""Native MCP surface for peers; transport and provider never confer authority."""

from .peer_decisions import PeerDecisions
from .peer_tasks import PeerTasks


def register(mcp, peers, actor):
    tasks, decisions = PeerTasks(peers), PeerDecisions(peers)

    def current():
        identity = actor()
        with peers.store.transaction() as db:
            peers.require_schema(db)
        return identity

    @mcp.tool()
    def peer_team(
        name: str, purpose: str, aliases: list[str], request_key: str
    ) -> dict:
        """Form a flat team of explicitly selected enrolled peers, including yourself. No leader is created."""
        return peers.create(current(), name, purpose, aliases, request_key)

    @mcp.tool()
    def peer_team_state(
        team_id: str,
        revision: int,
        state: str,
        user_instruction: str,
        evidence: str = "",
    ) -> dict:
        """Record explicit user-directed pause/resume/closure. Closed teams require no unfinished tasks; done also requires integrated evidence."""
        return peers.set_state(
            current(), team_id, revision, state, user_instruction, evidence
        )

    @mcp.tool()
    def peer_sync(team_id: str, after: int | None = None, limit: int = 20) -> dict:
        """Read your bounded inbox and current roster/board/tasks. Continue pages; receipt is not agreement."""
        return peers.sync(current(), team_id, after=after, limit=limit)

    @mcp.tool()
    def peer_ack(team_id: str, epoch: int, through_seq: int) -> dict:
        """Acknowledge retrieved context. A partial page cannot complete a new synchronization epoch."""
        return peers.acknowledge(current(), team_id, epoch, through_seq)

    @mcp.tool()
    def peer_member(
        team_id: str,
        alias: str,
        revision: int,
        lane: str | None = None,
        state: str | None = None,
        paths: list[str] | None = None,
        user_instruction: str = "",
    ) -> dict:
        """Set your continuing workstream/scope/activity. Other-peer changes require explicit user direction. Inactive keeps context."""
        return peers.update_member(
            current(),
            team_id,
            alias,
            revision,
            lane=lane,
            state=state,
            paths=paths,
            user_instruction=user_instruction,
        )

    @mcp.tool()
    def peer_board(
        team_id: str,
        revision: int,
        current_work: str,
        next_step: str,
        blockers: str = "",
        handoffs: str = "",
    ) -> dict:
        """Update only your board section. Describe your own workstream and next action; never another peer's progress."""
        return peers.board(
            current(), team_id, revision, current_work, next_step, blockers, handoffs
        )

    @mcp.tool()
    def peer_message(
        team_id: str,
        kind: str,
        body: str,
        request_key: str,
        recipients: list[str] | None = None,
    ) -> dict:
        """Send direct/handoff to named peers, or broadcast milestone/blocker/user_instruction. Share relevant context, not private transcripts."""
        return peers.message(current(), team_id, kind, body, request_key, recipients)

    @mcp.tool()
    def peer_receipts(team_id: str, message_id: str) -> dict:
        """Inspect per-recipient delivery, retrieval and explicit acknowledgement; none prove completed work."""
        return peers.receipts(current(), team_id, message_id)

    @mcp.tool()
    def peer_task_create(
        team_id: str,
        owner: str,
        reviewer: str,
        brief: str,
        acceptance: str,
        request_key: str,
        paths: list[str] | None = None,
    ) -> dict:
        """Request scoped work with an executor and a different enrolled reviewer. Empty paths means read-only."""
        return tasks.create(
            current(), team_id, owner, reviewer, brief, acceptance, request_key, paths
        )

    @mcp.tool()
    def peer_task_get(task_id: str) -> dict:
        """Read full task instructions, current evidence, ownership and history before acting."""
        return tasks.get(current(), task_id)

    @mcp.tool()
    def peer_task_take(team_id: str) -> dict:
        """After sync and acknowledgement, atomically claim/resume your next task. Any enrolled peer may execute."""
        return {"task": tasks.take(current(), team_id)}

    @mcp.tool()
    def peer_task_report(
        task_id: str,
        revision: int,
        result: str,
        evidence: str,
        blocked: bool = False,
        changes: dict | None = None,
    ) -> dict:
        """Current executor preserves evidence for independent review. A matching late result is allowed while inactive."""
        return tasks.report(
            current(),
            task_id,
            revision,
            result,
            evidence,
            blocked=blocked,
            changes=changes,
        )

    @mcp.tool()
    def peer_task_review(
        task_id: str, revision: int, decision: str, explanation: str
    ) -> dict:
        """Designated independent reviewer accepts, revises or blocks, citing evidence. At most two revisions."""
        return tasks.review(current(), task_id, revision, decision, explanation)

    @mcp.tool()
    def peer_task_release(
        task_id: str, revision: int, reason: str, evidence: str
    ) -> dict:
        """Executor explicitly stops its outstanding claim and preserves evidence before another writer may take over."""
        return tasks.release(current(), task_id, revision, reason, evidence)

    @mcp.tool()
    def peer_task_handoff(
        task_id: str,
        revision: int,
        reason: str,
        owner: str | None = None,
        reviewer: str | None = None,
    ) -> dict:
        """Transfer agreed task responsibility. Cannot replace an in-flight executor without its release."""
        return tasks.handoff(
            current(),
            task_id,
            revision,
            owner_alias=owner,
            reviewer_alias=reviewer,
            reason=reason,
        )

    @mcp.tool()
    def peer_task_recover(
        task_id: str, revision: int, user_instruction: str, stop_evidence: str
    ) -> dict:
        """Recover a stranded inactive writer ONLY with explicit user authorization and evidence it stopped. Stale heartbeat is insufficient."""
        return tasks.recover(
            current(), task_id, revision, user_instruction, stop_evidence
        )

    @mcp.tool()
    def peer_task_cancel(task_id: str, revision: int, reason: str) -> dict:
        """Requester/executor cancels scoped work, preserving history. Cannot cancel someone else's in-flight claim."""
        return tasks.cancel(current(), task_id, revision, reason)

    @mcp.tool()
    def peer_propose(
        team_id: str,
        topic: str,
        proposal: str,
        affected: list[str],
        request_key: str,
        supersedes: str | None = None,
        user_instruction: str = "",
    ) -> dict:
        """Record a consequential cross-workstream proposal for explicit agreement by named affected peers."""
        return decisions.propose(
            current(),
            team_id,
            topic,
            proposal,
            affected,
            request_key,
            supersedes,
            user_instruction,
        )

    @mcp.tool()
    def peer_decision_get(decision_id: str) -> dict:
        """Read the full proposal and each affected peer's own response."""
        return decisions.get(current(), decision_id)

    @mcp.tool()
    def peer_respond(decision_id: str, revision: int, response: str, note: str) -> dict:
        """Record only your agreement or objection with a reason. Inbox acknowledgement is never agreement."""
        return decisions.respond(current(), decision_id, revision, response, note)
