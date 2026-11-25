"""Explicit native-conversation cooldowns; never a provider quota or online probe."""

from contextlib import closing
from datetime import datetime, timezone
import json
import math
import time

from .schema import has_availability, has_quota
from .store import TeamError, checked


def parse_resume_after(value):
    """An absolute Unix timestamp or ISO date with an explicit timezone; None is unknown."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise TeamError("Reset time must be an epoch or ISO timestamp with timezone")
    try:
        stamp = float(value)
    except (ValueError, TypeError, OverflowError):
        try:
            date = datetime.fromisoformat(value)
            if date.utcoffset() is None:
                raise ValueError("Missing timezone")
            stamp = date.timestamp()
        except (ValueError, TypeError, OverflowError) as error:
            raise TeamError(
                "Reset time needs an epoch or ISO timestamp with timezone, e.g. 2026-11-24T08:00:00Z"
            ) from error
    if not math.isfinite(stamp) or stamp < 0:
        raise TeamError("Reset time must be a finite, nonnegative Unix timestamp")
    try:
        datetime.fromtimestamp(stamp, timezone.utc)
    except (OverflowError, OSError, ValueError) as error:
        raise TeamError("Reset time is outside the supported calendar range") from error
    return stamp


def read_availability(db, actor, *, now=None):
    """Read without installing optional schema or changing an expired record."""
    stamp = time.time() if now is None else now
    row = (
        db.execute(
            "SELECT status,resume_after,reason,revision,updated,recorded_by FROM member_availability WHERE member_id=?",
            (actor,),
        ).fetchone()
        if has_availability(db)
        else None
    )
    result = (
        dict(row)
        if row
        else {
            "status": "ready",
            "resume_after": None,
            "reason": "",
            "revision": 0,
            "updated": None,
            "recorded_by": "",
        }
    )
    result["recorded"] = row is not None
    result["held"] = result["status"] == "cooldown" and (
        result["resume_after"] is None or stamp < result["resume_after"]
    )
    result["resume_due"] = result["status"] == "cooldown" and not result["held"]
    quota = (
        db.execute(
            "SELECT posture,availability_revision,observations FROM member_quota WHERE member_id=?",
            (actor,),
        ).fetchone()
        if has_quota(db)
        else None
    )
    result["posture"] = (
        "unknown"
        if row is None
        else "cooldown"
        if result["status"] == "cooldown"
        else "normal"
    )
    # A pre-reload client only updates member_availability. Its newer explicit ready/
    # cooldown instruction wins, while old dated observations remain inspectable.
    if quota and row and quota["availability_revision"] == result["revision"]:
        result["posture"] = quota["posture"]
    result["observations"] = []
    for observation in json.loads(quota["observations"]) if quota else []:
        stale = (
            stamp < observation["observed_at"]
            or stamp >= observation["expires_at"]
            or (
                observation["resets_at"] is not None
                and stamp >= observation["resets_at"]
            )
        )
        result["observations"].append(
            {
                **observation,
                "stale": stale,
                "age_seconds": max(0, stamp - observation["observed_at"]),
                "current_remaining": None if stale else observation["remaining"],
            }
        )
    return result


def conserve_guidance(records):
    """Only explicit, effective member posture informs this team-scoped advisory."""
    if any(r["posture"] == "conserve" or r["status"] == "cooldown" for r in records):
        return {
            "conserve_guidance": (
                "Finish or park one increment at a tested boundary. Checkpoint files, checks "
                "and one next step on your peer_board. agy peers verify their own waits. If unattended "
                "and blocked, record the question once and pause that workstream."
            )
        }
    return {}


def validate_observations(values, now):
    """Bounded secret-free metadata supplied by the native client/user, not a collector."""
    if not isinstance(values, list) or len(values) > 8:
        raise TeamError("Observations must be a list of at most eight quota windows")
    allowed = {
        "source",
        "scope",
        "bucket",
        "observed_at",
        "expires_at",
        "remaining",
        "unit",
        "resets_at",
    }
    result, seen = [], set()
    for value in values:
        if not isinstance(value, dict) or set(value) - allowed:
            raise TeamError(
                "Observation contains unsupported fields; supply quota metadata only"
            )
        source = checked(value.get("source"), "Observation source", 200)
        scope = value.get("scope")
        if not isinstance(scope, str) or scope not in {"account", "model", "window"}:
            raise TeamError("Observation scope must be account, model or window")
        bucket = checked(value.get("bucket"), "Non-secret quota bucket label", 120)
        if (scope, bucket) in seen:
            raise TeamError(
                "Duplicate scope/bucket; supply one snapshot per quota window"
            )
        seen.add((scope, bucket))
        observed = parse_resume_after(value.get("observed_at"))
        expires = parse_resume_after(value.get("expires_at"))
        reset = parse_resume_after(value.get("resets_at"))
        if observed is None or expires is None or observed > now or expires <= observed:
            raise TeamError(
                "Observation needs a non-future observed_at and a later expires_at, both timezone-aware or epochs"
            )
        if reset is not None and reset <= observed:
            raise TeamError("Observation reset must be later than its observation time")
        remaining, unit = value.get("remaining"), value.get("unit")
        if remaining is None:
            if unit is not None:
                raise TeamError("An unknown remaining amount must omit its unit")
        else:
            try:
                valid_amount = (
                    type(remaining) in (int, float)
                    and math.isfinite(remaining)
                    and remaining >= 0
                )
            except OverflowError:
                valid_amount = False
            if not valid_amount:
                raise TeamError(
                    "Remaining amount must be a finite nonnegative number, or unknown"
                )
            unit = checked(unit, "Remaining amount unit", 40)
            if unit not in {"percent", "tokens", "requests", "credits", "USD"}:
                raise TeamError(
                    "Remaining unit must be percent, tokens, requests, credits or USD"
                )
            if unit == "percent" and remaining > 100:
                raise TeamError("Remaining percent must be between 0 and 100")
        result.append(
            {
                "source": source,
                "scope": scope,
                "bucket": bucket,
                "observed_at": observed,
                "expires_at": expires,
                "resets_at": reset,
                "remaining": remaining,
                "unit": unit,
            }
        )
    return result


def describe_availability(value):
    if not value["recorded"]:
        return "no quota restriction recorded"
    if value["resume_due"]:
        return "recorded reset time passed; provider quota not yet verified"
    if value["held"]:
        if value["resume_after"] is None:
            return "cooldown; reset time unknown, needs explicit recovery"
        return f"cooldown until {datetime.fromtimestamp(value['resume_after']).astimezone().isoformat()}"
    if value.get("posture") == "conserve":
        return "conserve; optional milestones wait for necessary work"
    if value.get("posture") == "unknown":
        return "unknown quota; native continuation is not held"
    return "marked ready"


def describe_observations(value):
    lines = []
    for item in value.get("observations", []):
        amount = (
            "unknown (stale snapshot)"
            if item["stale"]
            else (
                "unknown"
                if item["current_remaining"] is None
                else f"{item['current_remaining']} {item['unit']} (observed, not live)"
            )
        )
        observed = datetime.fromtimestamp(item["observed_at"]).astimezone().isoformat()
        expires = datetime.fromtimestamp(item["expires_at"]).astimezone().isoformat()
        reset = (
            "unknown"
            if item["resets_at"] is None
            else datetime.fromtimestamp(item["resets_at"]).astimezone().isoformat()
        )
        lines.append(
            f"{item['scope']}/{item['bucket']}: {amount}; source {item['source']}; observed {observed} ({int(item['age_seconds'])}s ago), expires {expires}, reset {reset}"
        )
    return lines


def member_progress(db, team, actor, *, now=None):
    """Keep unread messages distinct from tasks awaiting an action after being read."""
    stamp = time.time() if now is None else now
    messages = db.execute(
        "SELECT count(*),min(m.created) FROM peer_deliveries d JOIN peer_messages m ON m.id=d.message_id "
        "WHERE m.team_id=? AND d.recipient=? AND d.received_at IS NULL AND m.kind IN ('direct','handoff','blocker','user_instruction','lifecycle','quota')",
        (team, actor),
    ).fetchone()
    tasks = db.execute(
        "SELECT count(*),min(updated) FROM peer_tasks WHERE team_id=? AND ((owner=? AND state='ready') OR (reviewer=? AND state='review'))",
        (team, actor, actor),
    ).fetchone()
    from .quota_store import native_member_view

    return {
        "availability": read_availability(db, actor, now=stamp),
        "native_quota": native_member_view(db, actor, now=stamp),
        "waiting": {
            "unreceived_messages": messages[0],
            "message_since": messages[1],
            "message_age_seconds": max(0, stamp - messages[1])
            if messages[1] is not None
            else None,
            "pending_tasks": tasks[0],
            "task_since": tasks[1],
            "task_age_seconds": max(0, stamp - tasks[1])
            if tasks[1] is not None
            else None,
        },
    }


def _save_availability(db, actor, revision, status, reason, reset, stamp, recorded_by):
    db.execute(
        "INSERT INTO member_availability(member_id,status,resume_after,reason,revision,updated,recorded_by) VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(member_id) DO UPDATE SET status=excluded.status,resume_after=excluded.resume_after,reason=excluded.reason,revision=excluded.revision,updated=excluded.updated,recorded_by=excluded.recorded_by",
        (actor, status, reset, reason, revision + 1, stamp, recorded_by),
    )


class Availability:
    def __init__(self, store):
        self.store = store

    def get(self, actor):
        with closing(self.store.connect()) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            self.store.member(db, actor)
            return read_availability(db, actor)

    def set(
        self, actor, revision, status, reason, resume_after=None, *, recorded_by=None
    ):
        if status not in {"ready", "cooldown"}:
            raise TeamError("Availability must be ready or cooldown, not online status")
        reason = checked(reason, "Availability reason", 2000)
        reset = parse_resume_after(resume_after)
        if status == "ready" and reset is not None:
            raise TeamError("Ready availability must not include a reset time")
        with self.store.transaction() as db:
            member = self.store.member(db, actor)
            if not has_availability(db):
                raise TeamError(
                    "Cooldown recording needs native-team migrate; read-only inspection did not install it"
                )
            old = read_availability(db, actor)
            if type(revision) is not int or revision != old["revision"]:
                raise TeamError("Stale availability revision; inspect before updating")
            _save_availability(
                db,
                actor,
                revision,
                status,
                reason,
                reset,
                time.time(),
                recorded_by or member["alias"],
            )
            return read_availability(db, actor)

    def record(
        self,
        actor,
        revision,
        posture,
        reason,
        resume_after=None,
        *,
        observations=None,
        recorded_by=None,
    ):
        """One atomic update, sharing the legacy revision guard; never infers a reset."""
        if posture not in {"unknown", "normal", "conserve", "cooldown"}:
            raise TeamError(
                "Quota posture must be unknown, normal, conserve or cooldown"
            )
        reason = checked(reason, "Quota reason", 2000)
        reset = parse_resume_after(resume_after)
        if posture != "cooldown" and reset is not None:
            raise TeamError("Only cooldown may defer native continuation")
        stamp = time.time()
        observations = (
            None if observations is None else validate_observations(observations, stamp)
        )
        with self.store.transaction() as db:
            member = self.store.member(db, actor)
            if not has_availability(db) or not has_quota(db):
                raise TeamError(
                    "Quota recording needs native-team migrate; inspection does not install it"
                )
            old = read_availability(db, actor, now=stamp)
            if type(revision) is not int or revision != old["revision"]:
                raise TeamError("Stale availability revision; inspect before updating")
            prior = db.execute(
                "SELECT observations FROM member_quota WHERE member_id=?", (actor,)
            ).fetchone()
            payload = (
                json.dumps(observations, ensure_ascii=False)
                if observations is not None
                else prior[0]
                if prior
                else "[]"
            )
            _save_availability(
                db,
                actor,
                revision,
                "cooldown" if posture == "cooldown" else "ready",
                reason,
                reset,
                stamp,
                recorded_by or member["alias"],
            )
            db.execute(
                "INSERT INTO member_quota(member_id,posture,availability_revision,observations) VALUES(?,?,?,?) "
                "ON CONFLICT(member_id) DO UPDATE SET posture=excluded.posture,availability_revision=excluded.availability_revision,observations=excluded.observations",
                (actor, posture, revision + 1, payload),
            )
            return read_availability(db, actor, now=stamp)
