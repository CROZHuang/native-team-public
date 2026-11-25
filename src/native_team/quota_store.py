"""Native metadata ingestion and bounded, operator-authorized quota transitions.

Automatic messages are labelled as monitor observations, never as peer reports.
No task ownership, vote, receipt, login or native conversation is changed here.
"""

import json
from pathlib import Path
import re
import time

from .availability import _save_availability, read_availability, validate_observations
from .claude_quota import (
    SOURCE as CLAUDE_SOURCE,
    TARGETS as CLAUDE_TARGETS,
    WINDOWS as CLAUDE_WINDOWS,
    read_private_metadata,
)
from .peers import PeerStore
from .quota_capture import finite
from .schema import has_native_quota
from .store import CLAUDE_ALIASES, TeamError, new_id

RECORDER = "native quota monitor"
FRESH_SECONDS = 300


def applicable(window, provider, model):
    if window.get("disabled"):
        return False
    if provider == "claude":
        return window["bucket"] in CLAUDE_WINDOWS
    if provider == "codex":
        return window.get("group") == "codex" or bool(
            model and window.get("model") == model
        )
    family = next(
        (x for x in ("gemini", "claude", "gpt") if model.casefold().startswith(x)), None
    )
    if family in {"claude", "gpt"} and re.match(
        r"^3p(?:[-_/ .]|$)", window["bucket"], re.I
    ):
        return True
    return bool(family and re.match(rf"^{family}(?:[-_/ .]|$)", window["bucket"], re.I))


def source_view(row, *, now):
    item = dict(row)
    payload = json.loads(item["snapshot"])
    stale = now < item["observed_at"] or now - item["observed_at"] >= FRESH_SECONDS
    return {
        "provider": item["provider"],
        "source": payload["source"],
        "account": ("账户 " + item["account_key"][:8])
        if item["account_key"]
        else "账户未识别",
        "account_key": item["account_key"],
        "model": item["model"],
        "association": item["association"],
        "observed_at": item["observed_at"],
        "received_at": item["received_at"],
        "stale": stale,
        "windows": [
            {
                **window,
                "applies": applicable(window, item["provider"], item["model"]),
                "stale": stale
                or (
                    "observed_at" in window
                    and not 0 <= now - window["observed_at"] < FRESH_SECONDS
                )
                or (window.get("resets_at") is not None and now >= window["resets_at"]),
            }
            for window in payload["windows"]
        ],
    }


def native_member_view(db, member_id, *, now):
    if not has_native_quota(db):
        return None
    row = db.execute(
        "SELECT * FROM native_quota_sources WHERE member_id=?", (member_id,)
    ).fetchone()
    if row is None:
        return None
    if (
        row["provider"] == "claude"
        and not db.execute(CLAUDE_TARGETS + " AND m.id=?", (member_id,)).fetchone()
    ):
        return None
    view = source_view(row, now=now)
    view.pop("account_key")
    policy = db.execute(
        "SELECT posture,reason,resets_at,updated FROM native_quota_policy WHERE member_id=?",
        (member_id,),
    ).fetchone()
    view["policy"] = dict(policy) if policy else None
    return view


def shared_claude_account(state, db, *, now, team_id=None):
    """An owner-confirmed display group. Native account identity and policy remain per source."""
    try:
        spec = read_private_metadata(
            Path(state) / "quota/claude-shared-account.json", 4096
        )
        aliases = spec["aliases"]
        if (
            not isinstance(spec["team_id"], str)
            or not isinstance(aliases, list)
            or not 1 <= len(aliases) <= len(CLAUDE_ALIASES)
            or any(alias not in CLAUDE_ALIASES for alias in aliases)
            or len(set(aliases)) != len(aliases)
        ):
            raise ValueError
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError):
        return {"error": "本机 Claude 共享账号配置无效；请核对团队 ID 和成员别名。"}
    if team_id is not None and team_id != spec["team_id"]:
        return None
    team = db.execute(
        "SELECT id,name,project FROM peer_teams WHERE id=? AND state IN ('active','paused')",
        (spec["team_id"],),
    ).fetchone()
    if team is None:
        return None
    members = []
    samples = {bucket: [] for bucket in CLAUDE_WINDOWS}
    for member in db.execute(
        """SELECT n.id,n.alias FROM peer_members p JOIN members n ON n.id=p.member_id
           WHERE p.team_id=? AND p.state='active' AND n.provider='claude' ORDER BY n.alias""",
        (team["id"],),
    ):
        alias = member["alias"]
        if alias not in aliases:
            continue
        members.append(alias)
        data = native_member_view(db, member["id"], now=now)
        if data and data["association"] == "verified":
            for window in data["windows"]:
                if window.get("observed_at") is not None:
                    samples[window["bucket"]].append({**window, "source_alias": alias})
    if not members:
        return None
    windows = []
    for bucket, candidates in samples.items():
        # Select each subscription period independently. Equal timestamps retain stable alias order.
        windows.append(
            max(
                candidates,
                key=lambda w: (
                    not w["stale"],
                    w["remaining"] is not None,
                    w["observed_at"],
                ),
                default={
                    "bucket": bucket,
                    "remaining": None,
                    "resets_at": None,
                    "observed_at": None,
                    "source_alias": None,
                    "stale": False,
                },
            )
        )
    return {
        "team_id": team["id"],
        "team": team["name"],
        "project": team["project"],
        "members": members,
        "windows": windows,
    }


def claude_display_sources(account):
    """The local monitor publishes only selectors. Balances come from the viewed team's own rows."""
    if not account or "error" in account:
        return None
    return {
        "team_id": account["team_id"],
        "members": account["members"],
        "sources": {w["bucket"]: w["source_alias"] for w in account["windows"]},
        "fresh_seconds": FRESH_SECONDS,
    }


def _validate_snapshot(snapshot, now):
    required = {"provider", "source", "account_key", "windows"}
    if not finite(now) or now < 0:
        raise TeamError("Invalid native snapshot time")
    if not isinstance(snapshot, dict) or not required <= set(snapshot):
        raise TeamError("Incomplete native quota snapshot")
    if set(snapshot) - (required | {"model", "session_key", "ordinary_allowed"}):
        raise TeamError("Native quota snapshot contains unsupported fields")
    if snapshot["provider"] not in {"codex", "agy", "claude"}:
        raise TeamError("Unsupported native quota provider")
    if (
        snapshot["source"]
        != {
            "agy": "agy native statusline",
            "codex": "Codex native account/rateLimits/read",
            "claude": CLAUDE_SOURCE,
        }[snapshot["provider"]]
    ):
        raise TeamError("Unknown native quota source")
    if snapshot["account_key"] is not None and (
        not isinstance(snapshot["account_key"], str)
        or not re.fullmatch(r"[a-f0-9]{64}", snapshot["account_key"])
    ):
        raise TeamError("Quota accounts must use a local pseudonym")
    if (
        snapshot.get("ordinary_allowed") is not None
        and type(snapshot["ordinary_allowed"]) is not bool
    ):
        raise TeamError("Native usage permission must be boolean or unknown")
    windows = snapshot["windows"]
    if not isinstance(windows, list) or len(windows) > 64:
        raise TeamError("Invalid native quota windows")
    if snapshot["provider"] == "claude":
        if set(snapshot) != required or snapshot["account_key"] is not None:
            raise TeamError("Claude status lines provide session observations only")
        for w in windows:
            if (
                not isinstance(w, dict)
                or not isinstance(w.get("bucket"), str)
                or w.get("bucket") not in CLAUDE_WINDOWS
                or set(w) - {"bucket", "remaining", "resets_at", "observed_at"}
                or (
                    "observed_at" in w
                    and not (
                        finite(w["observed_at"]) and 0 <= w["observed_at"] <= now + 2
                    )
                )
                or (w.get("remaining") is not None and "observed_at" not in w)
            ):
                raise TeamError("Invalid Claude subscription window")
    for window in windows:
        if (
            not isinstance(window, dict)
            or not isinstance(window.get("bucket"), str)
            or not 0 < len(window["bucket"]) <= 160
        ):
            raise TeamError("Invalid native quota bucket")
        allowed_fields = {
            "bucket",
            "remaining",
            "amount",
            "resets_at",
            "disabled",
            "group",
            "model",
            "duration_minutes",
            "blocked",
        }
        if snapshot["provider"] == "claude":
            allowed_fields.add("observed_at")
        if set(window) - allowed_fields:
            raise TeamError("Native quota window contains unsupported fields")
        remaining = window.get("remaining")
        if remaining is not None and not (finite(remaining) and 0 <= remaining <= 100):
            raise TeamError("Invalid native remaining percentage")
        reset = window.get("resets_at")
        if reset is not None and not (finite(reset) and reset >= 0):
            raise TeamError("Invalid native reset timestamp")
        for flag in ("disabled", "blocked"):
            if flag in window and type(window[flag]) is not bool:
                raise TeamError("Native quota flags must be boolean")
        for label in ("model", "group"):
            if label in window and (
                not isinstance(window[label], str) or len(window[label]) > 160
            ):
                raise TeamError("Invalid native quota label")
        amount = window.get("amount")
        if amount is not None and not (finite(amount) and amount >= 0):
            raise TeamError("Invalid native quota amount")
    if len({w["bucket"] for w in windows}) != len(windows):
        raise TeamError("Duplicate native quota buckets")


class QuotaStore:
    def __init__(self, store):
        self.store = store

    def ingest(
        self,
        member_id,
        source_id,
        connection,
        session_key,
        model,
        snapshot,
        observed_at,
        received_at,
        *,
        now=None,
    ):
        stamp = time.time() if now is None else now
        _validate_snapshot(snapshot, stamp)
        if not all(
            isinstance(x, str) and 0 < len(x) <= 256
            for x in (source_id, connection, session_key)
        ):
            raise TeamError("Missing verified native quota association")
        if not isinstance(model, str) or len(model) > 120:
            raise TeamError("Invalid quota model metadata")
        if (
            not all(
                finite(x) and 0 <= x <= stamp + 2 for x in (observed_at, received_at)
            )
            or received_at < observed_at
            or any(w.get("observed_at", 0) > observed_at for w in snapshot["windows"])
        ):
            raise TeamError("Invalid native observation times")
        with self.store.transaction() as db:
            if not has_native_quota(db):
                raise TeamError(
                    "Native quota collection requires an explicit ledger migration"
                )
            member = self.store.member(db, member_id)
            if member["provider"] != snapshot["provider"]:
                raise TeamError("Quota source does not match the enrolled provider")
            if (
                member["provider"] == "claude"
                and not db.execute(
                    CLAUDE_TARGETS + " AND m.id=?", (member_id,)
                ).fetchone()
            ):
                return False
            old = db.execute(
                "SELECT * FROM native_quota_sources WHERE member_id=?", (member_id,)
            ).fetchone()
            if (
                old
                and old["connection"] == connection
                and old["session_key"] != session_key
            ):
                db.execute(
                    "UPDATE native_quota_sources SET association='conversation_changed' WHERE member_id=?",
                    (member_id,),
                )
                return False
            if (
                old
                and old["source_id"] == source_id
                and received_at < old["received_at"]
            ):
                return False
            db.execute(
                """INSERT INTO native_quota_sources
                (member_id,source_id,provider,connection,session_key,account_key,model,observed_at,received_at,snapshot,association)
                VALUES(?,?,?,?,?,?,?,?,?,?,'verified') ON CONFLICT(member_id) DO UPDATE SET
                source_id=excluded.source_id,provider=excluded.provider,connection=excluded.connection,
                session_key=excluded.session_key,account_key=excluded.account_key,model=excluded.model,
                observed_at=excluded.observed_at,received_at=excluded.received_at,snapshot=excluded.snapshot,
                association=excluded.association""",
                (
                    member_id,
                    source_id,
                    snapshot["provider"],
                    connection,
                    session_key,
                    snapshot["account_key"],
                    model,
                    observed_at,
                    received_at,
                    json.dumps(snapshot, ensure_ascii=False, allow_nan=False),
                ),
            )
            return True

    def reconcile(self, *, now=None, threshold=10):
        stamp = time.time() if now is None else now
        if not finite(threshold) or not 0 <= threshold <= 30:
            raise TeamError("Quota warning threshold must be 0–30 percent")
        transitions = []
        with self.store.transaction() as db:
            if not has_native_quota(db):
                return []
            # Subscription utilization alone does not signal whether Claude can accept work.
            rows = db.execute("""SELECT s.* FROM native_quota_sources s WHERE s.provider IN ('codex','agy') AND EXISTS(
                SELECT 1 FROM peer_members pm JOIN peer_teams t ON t.id=pm.team_id
                WHERE pm.member_id=s.member_id AND pm.state='active' AND t.state IN ('active','paused'))""").fetchall()
            for row in rows:
                meta = json.loads(row["snapshot"])
                prior = db.execute(
                    "SELECT * FROM native_quota_policy WHERE member_id=?",
                    (row["member_id"],),
                ).fetchone()
                view = source_view(row, now=stamp)
                selected = [
                    w for w in view["windows"] if w["applies"] and not w["stale"]
                ]
                fresh = row["association"] == "verified" and not view["stale"]
                known = [w for w in selected if w.get("remaining") is not None]
                blocked = fresh and (
                    (
                        meta.get("ordinary_allowed") is False
                        and row["provider"] == "codex"
                    )
                    or any(w.get("blocked") for w in selected)
                    or (
                        row["provider"] == "agy"
                        and any(w["remaining"] == 0 for w in known)
                    )
                )
                positive = (
                    fresh and bool(known) and all(w["remaining"] > 0 for w in known)
                )
                if row["provider"] == "codex":
                    positive = (
                        fresh and meta.get("ordinary_allowed") is True and not blocked
                    )
                evidence_row, evidence_meta, evidence_view = row, meta, view
                # One shared account's exhaustion affects its matching model bucket,
                # even if another native window cannot produce its own final report.
                related = [
                    r
                    for r in rows
                    if row["association"] == "verified"
                    and (
                        r["member_id"] == row["member_id"]
                        or (
                            row["account_key"]
                            and r["account_key"] == row["account_key"]
                        )
                    )
                    and r["provider"] == row["provider"]
                    and r["association"] == "verified"
                ]
                for candidate in sorted(
                    related, key=lambda r: r["observed_at"], reverse=True
                ):
                    if (
                        candidate["observed_at"] > stamp
                        or stamp - candidate["observed_at"] >= FRESH_SECONDS
                    ):
                        continue
                    candidate_meta = json.loads(candidate["snapshot"])
                    candidate_view = source_view(candidate, now=stamp)
                    affected = [
                        w
                        for w in candidate_view["windows"]
                        if applicable(w, row["provider"], row["model"])
                        and not w["stale"]
                    ]
                    zeros = [
                        w
                        for w in affected
                        if w.get("remaining") == 0 or w.get("blocked")
                    ]
                    if (row["provider"] == "agy" and zeros) or (
                        row["provider"] == "codex"
                        and (
                            candidate_meta.get("ordinary_allowed") is False
                            or any(w.get("blocked") for w in affected)
                        )
                    ):
                        blocked, positive = True, False
                        evidence_row, evidence_meta, evidence_view = (
                            candidate,
                            candidate_meta,
                            candidate_view,
                        )
                        known = affected
                        break
                    candidate_known = [
                        w for w in affected if w.get("remaining") is not None
                    ]
                    candidate_positive = (
                        candidate_meta.get("ordinary_allowed") is True
                        if row["provider"] == "codex"
                        else bool(candidate_known)
                        and all(w["remaining"] > 0 for w in candidate_known)
                    )
                    if (
                        not blocked
                        and candidate_positive
                        and candidate_known
                        and min(w["remaining"] for w in candidate_known) <= threshold
                    ):
                        if not known or min(
                            w["remaining"] for w in candidate_known
                        ) < min(
                            w["remaining"]
                            for w in known
                            if w.get("remaining") is not None
                        ):
                            positive = True
                            evidence_row, evidence_meta, evidence_view = (
                                candidate,
                                candidate_meta,
                                candidate_view,
                            )
                            known = candidate_known
                if prior and prior["posture"] == "cooldown" and not blocked:
                    # A cached positive value from another window cannot clear a zero.
                    # Recovery must come from the native source that established it.
                    driver = next(
                        (
                            r
                            for r in related
                            if r["source_id"] == prior["blocking_source"]
                        ),
                        None,
                    )
                    if (
                        driver
                        and driver["observed_at"] > prior["evidence_at"]
                        and driver["observed_at"] <= stamp
                        and stamp - driver["observed_at"] < FRESH_SECONDS
                    ):
                        dmeta = json.loads(driver["snapshot"])
                        dview = source_view(driver, now=stamp)
                        dknown = [
                            w
                            for w in dview["windows"]
                            if applicable(w, row["provider"], row["model"])
                            and not w["stale"]
                            and w.get("remaining") is not None
                        ]
                        positive = (
                            dmeta.get("ordinary_allowed") is True
                            if row["provider"] == "codex"
                            else bool(dknown)
                            and all(w["remaining"] > 0 for w in dknown)
                        )
                        if row["provider"] == "agy":
                            positive = positive and set(
                                json.loads(prior["blocking_windows"])
                            ) <= {w["bucket"] for w in dknown}
                        if positive:
                            evidence_row, evidence_meta, evidence_view = (
                                driver,
                                dmeta,
                                dview,
                            )
                            known = dknown
                    else:
                        positive = False
                resets = [
                    w.get("resets_at")
                    for w in known
                    if w.get("remaining") == 0 or w.get("blocked")
                ]
                reset = (
                    max(resets)
                    if resets and all(r is not None for r in resets)
                    else None
                )
                if blocked:
                    posture, reason = (
                        "cooldown",
                        "原生客户端报告当前模型额度受限；自动等待新的恢复证据。",
                    )
                elif (
                    prior
                    and prior["posture"] == "cooldown"
                    and not (
                        positive and evidence_row["observed_at"] > prior["evidence_at"]
                    )
                ):
                    posture, reset = "cooldown", prior["resets_at"]
                    reason = (
                        "曾确认额度耗尽；等待原生客户端刷新，重置时间经过不等于已恢复。"
                    )
                elif positive:
                    posture = (
                        "conserve"
                        if known and min(w["remaining"] for w in known) <= threshold
                        else "normal"
                    )
                    reason = (
                        "原生额度偏低；请保留检查点并减少非必要交接。"
                        if posture == "conserve"
                        else "原生客户端提供了当前可用额度；这不代表会话在线。"
                    )
                else:
                    posture, reason = (
                        "unknown",
                        "额度数据未关联、缺失或已过期；未据此判断为零。",
                    )
                evidence = (
                    evidence_row["observed_at"]
                    if blocked or positive
                    else prior["evidence_at"]
                    if prior
                    else row["observed_at"]
                )
                blocking_windows = (
                    sorted(
                        w["bucket"]
                        for w in known
                        if w.get("remaining") == 0 or w.get("blocked")
                    )
                    if blocked
                    else json.loads(prior["blocking_windows"])
                    if prior and posture == "cooldown"
                    else []
                )
                if blocked and prior and prior["posture"] == "cooldown":
                    # A partial refresh cannot silently forget another exhausted bucket.
                    blocking_windows = sorted(
                        set(blocking_windows)
                        | set(json.loads(prior["blocking_windows"]))
                    )
                changed = not prior or (
                    prior["posture"],
                    prior["reason"],
                    prior["resets_at"],
                    prior["evidence_at"],
                ) != (posture, reason, reset, evidence)
                if not changed:
                    continue
                revision = prior["revision"] + 1 if prior else 1
                db.execute(
                    """INSERT INTO native_quota_policy(member_id,posture,reason,resets_at,updated,evidence_at,blocking_source,revision,blocking_windows)
                    VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(member_id) DO UPDATE SET posture=excluded.posture,
                    reason=excluded.reason,resets_at=excluded.resets_at,updated=excluded.updated,evidence_at=excluded.evidence_at,
                    blocking_source=excluded.blocking_source,revision=excluded.revision,blocking_windows=excluded.blocking_windows""",
                    (
                        row["member_id"],
                        posture,
                        reason,
                        reset,
                        stamp,
                        evidence,
                        evidence_row["source_id"]
                        if blocked
                        else prior["blocking_source"]
                        if prior and posture == "cooldown"
                        else None,
                        revision,
                        json.dumps(blocking_windows),
                    ),
                )
                availability = read_availability(db, row["member_id"], now=stamp)
                # Never release or replace a human/native peer's explicit hold.
                if not (
                    availability["status"] == "cooldown"
                    and availability["recorded_by"] != RECORDER
                ):
                    observations = [
                        {
                            "source": evidence_meta["source"],
                            "scope": "window",
                            "bucket": w["bucket"],
                            "observed_at": evidence_row["observed_at"],
                            "expires_at": evidence_row["observed_at"] + FRESH_SECONDS,
                            "remaining": w.get("remaining"),
                            "unit": "percent"
                            if w.get("remaining") is not None
                            else None,
                            "resets_at": w.get("resets_at")
                            if w.get("resets_at")
                            and w["resets_at"] > evidence_row["observed_at"]
                            else None,
                        }
                        for w in evidence_view["windows"]
                        if applicable(w, row["provider"], row["model"])
                    ][:8]
                    observations = validate_observations(
                        observations, max(stamp, evidence_row["observed_at"])
                    )
                    _save_availability(
                        db,
                        row["member_id"],
                        availability["revision"],
                        "cooldown" if posture == "cooldown" else "ready",
                        reason,
                        None,
                        stamp,
                        RECORDER,
                    )
                    db.execute(
                        """INSERT INTO member_quota(member_id,posture,availability_revision,observations)
                        VALUES(?,?,?,?) ON CONFLICT(member_id) DO UPDATE SET posture=excluded.posture,
                        availability_revision=excluded.availability_revision,observations=excluded.observations""",
                        (
                            row["member_id"],
                            posture,
                            availability["revision"] + 1,
                            json.dumps(observations, ensure_ascii=False),
                        ),
                    )
                if (
                    posture in {"cooldown", "conserve"}
                    and (not prior or prior["posture"] != posture)
                ) or (
                    prior
                    and prior["posture"] == "cooldown"
                    and posture in {"normal", "conserve"}
                ):
                    transitions.append(
                        {
                            "member_id": row["member_id"],
                            "posture": posture,
                            "reason": reason,
                            "account_key": row["account_key"],
                        }
                    )
            self._notify(db, transitions)
        return transitions

    def _notify(self, db, transitions):
        grouped = {}
        for event in transitions:
            for row in db.execute(
                """SELECT t.id,m.alias FROM peer_members pm JOIN peer_teams t ON t.id=pm.team_id
                JOIN members m ON m.id=pm.member_id WHERE pm.member_id=? AND pm.state='active' AND t.state='active'""",
                (event["member_id"],),
            ):
                grouped.setdefault(
                    (row["id"], event["account_key"], event["posture"]), []
                ).append({**event, "alias": row["alias"]})
        for (team, _, posture), events in grouped.items():
            recipients = {
                r[0]
                for r in db.execute(
                    "SELECT member_id FROM peer_members WHERE team_id=? AND state='active'",
                    (team,),
                )
            }
            body = (
                "Native Team 自动额度监控（程序观测，不是模型报告）："
                + ", ".join(x["alias"] for x in events)
                + f" → {posture}。"
                + events[0]["reason"]
                + " 保留现有任务归属和检查点；其他成员可继续独立工作。接管在途写入仍需先确认原执行者已停止。"
            )
            PeerStore(self.store)._publish(
                db,
                events[0]["member_id"],
                team,
                "quota",
                body,
                "native-quota:" + new_id(),
                recipients,
            )
