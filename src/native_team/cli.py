import argparse
import json
from pathlib import Path
import sys
import time

from .store import CLIENT_PROVIDERS, Store, TeamError


def _print_claude_windows(windows):
    from .claude_quota import WINDOWS

    for window in windows:
        if window["remaining"] is None:
            remaining = "未提供"
        elif window["stale"]:
            remaining = "待刷新"
        else:
            remaining = f"剩余 {window['remaining']:g}%"
        reset = window.get("resets_at")
        suffix = (
            time.strftime(" · 重置 %m-%d %H:%M %Z", time.localtime(reset))
            if reset is not None
            else ""
        )
        print(f"    {WINDOWS[window['bucket']]}：{remaining}{suffix}")


def main():
    parser = argparse.ArgumentParser(
        description="Coordination for independent native CLI teammates"
    )
    parser.add_argument(
        "--state", type=Path, default=Path.home() / ".local/state/native-team"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "claude-quota-stream",
        help="Pass native status-line input to its configured renderer",
    )
    commands.add_parser(
        "agy-startup",
        help="Read-only native SessionStart reminder; unknown sessions are no-ops",
    )
    commands.add_parser(
        "migrate", help="Back up and add flat-team tables, preserving legacy work"
    )
    wait_parser = commands.add_parser(
        "wait",
        help="Wait for an enrolled agy or Claude Code peer through its native background command",
    )
    wait_parser.add_argument("--project", type=Path, default=Path.cwd())
    wait_parser.add_argument("--alias", required=True)
    wait_parser.add_argument("--timeout", type=int, default=3600)
    wait_parser.add_argument(
        "--mode", choices=["all", "actionable", "recovery"], default="all"
    )
    wait_parser.add_argument(
        "--coalesce",
        type=float,
        default=0,
        help="Quiet-window seconds, 0–30; bursts have a bounded maximum delay",
    )
    wait_parser.add_argument(
        "--not-before",
        help="Do not finish ready before this epoch or ISO timestamp with timezone",
    )
    cooldown = commands.add_parser(
        "availability",
        help="Inspect or explicitly record quota posture and dated observations",
    )
    cooldown.add_argument("--project", type=Path, default=Path.cwd())
    cooldown.add_argument("--alias", required=True)
    cooldown.add_argument(
        "--set",
        dest="availability_status",
        choices=["ready", "cooldown", "unknown", "normal", "conserve"],
    )
    cooldown.add_argument(
        "--resume-after",
        help="Known reset epoch or ISO timestamp with timezone; omit if unknown",
    )
    cooldown.add_argument("--reason", default="")
    cooldown.add_argument(
        "--observations-json",
        help="JSON list of dated, non-secret quota snapshots; [] clears; requires --set",
    )
    cooldown.add_argument("--json", action="store_true")
    quota = commands.add_parser(
        "quota",
        help="Inspect native quota collection and its enrolled account associations",
    )
    quota.add_argument("--json", action="store_true")
    toggle = quota.add_mutually_exclusive_group()
    toggle.add_argument(
        "--enable",
        action="store_true",
        help="Enable metadata-only collection in the existing monitor",
    )
    toggle.add_argument(
        "--disable",
        action="store_true",
        help="Stop collection; preserve records and explicit holds",
    )
    monitor = commands.add_parser(
        "monitor", help="Run persistent native Codex notifications; never drives agy"
    )
    monitor.add_argument("--codex", type=Path, default=Path.home() / ".local/bin/codex")
    mcp = commands.add_parser("mcp")
    mcp.add_argument("--client", choices=CLIENT_PROVIDERS, required=True)
    status = commands.add_parser(
        "status", help="Inspect progress without enrolling or waking clients"
    )
    status.add_argument("--project", type=Path, default=Path.cwd())
    status.add_argument(
        "--json",
        action="store_true",
        help="Includes this project’s task content; keep private",
    )
    teams = commands.add_parser(
        "teams", help="List team names and members across all projects, read-only"
    )
    teams.add_argument("--project", type=Path, help="Show only this project's teams")
    teams.add_argument("--json", action="store_true", help="Team directory as JSON")
    ui = commands.add_parser("ui", help="Open the local read-only team workbench")
    ui.add_argument(
        "--port",
        type=int,
        default=0,
        help="Loopback port (default: choose a free port)",
    )
    ui.add_argument(
        "--no-open", action="store_true", help="Print the URL without opening a browser"
    )
    args = parser.parse_args()
    try:
        if args.command == "claude-quota-stream":
            from .claude_quota import forward_statusline

            forward_statusline(sys.stdin.buffer, sys.stdout.buffer, args.state)
        elif args.command == "agy-startup":
            from .startup import read_session_start

            print(
                json.dumps(
                    read_session_start(sys.stdin, args.state), ensure_ascii=False
                )
            )
        elif args.command == "wait":
            from .waiting import wait

            result = wait(
                args.state,
                args.project,
                args.alias,
                args.timeout,
                mode=args.mode,
                coalesce=args.coalesce,
                not_before=args.not_before,
            )
            # Claude includes a completed background command's stdout in its conversation.
            provider = result.pop("provider")
            if provider == "claude":
                result = (
                    {"ready": True}
                    if result["ready"]
                    else {"ready": False, "reason": "deadline"}
                )
            print(json.dumps(result, ensure_ascii=False))
            if not result["ready"]:
                # An idle deadline is an expected outcome, not a new-work notification.
                raise SystemExit(124)
        elif args.command == "quota":
            from .quota_collector import set_enabled
            from .quota_store import RECORDER
            from .quota_view import quota_overview

            if args.enable or args.disable:
                set_enabled(args.state, args.enable)
            value = quota_overview(args.state)
            if args.json:
                print(json.dumps(value, ensure_ascii=False, indent=2))
            else:
                print("Native Team · 原生额度")
                print(
                    "自动采集："
                    + ("已启用" if value["collector"].get("enabled") else "未启用")
                )
                for provider in ("codex", "agy", "claude"):
                    status = value["collector"].get(provider, {})
                    print(f"  {provider}: {status.get('detail', '等待首次采集')}")
                shared = value["shared_claude_account"]
                shared_members = set()
                if shared and "error" in shared:
                    print("  " + shared["error"])
                elif shared:
                    print(
                        f"  {shared['team']} · Claude Code 共享订阅额度（{len(shared['members'])} 个成员）"
                    )
                    print("    成员：" + "、".join(shared["members"]))
                    _print_claude_windows(shared["windows"])
                    shared_members = {
                        (shared["project"], alias) for alias in shared["members"]
                    }
                for member in value["members"]:
                    data = member["native_quota"]
                    availability = member["availability"]
                    recovery = member.get("recovery")
                    watches = member.get("watches")
                    if (
                        member["provider"] == "claude"
                        and (member["project"], member["alias"]) in shared_members
                    ):
                        if availability["held"]:
                            print(
                                f"    {member['alias']} 已登记冷却："
                                + availability["reason"]
                            )
                        continue
                    explicit_hold = (
                        availability["status"] == "cooldown"
                        and availability["recorded_by"] != RECORDER
                    )
                    if not data:
                        print(f"  {member['alias']}: 尚未收到可关联的原生额度数据")
                    elif member["provider"] == "claude":
                        print(
                            f"  {member['alias']} · Claude Code 订阅额度（按会话观测）"
                        )
                        if availability["held"]:
                            print("    已登记冷却：" + availability["reason"])
                    else:
                        print(
                            f"  {member['alias']} · {data['account']} · {data['model'] or '模型未知'}"
                        )
                        posture = (
                            "cooldown"
                            if availability["held"]
                            else availability["posture"]
                        )
                        print(
                            "    状态："
                            + {
                                "cooldown": "冷却中",
                                "conserve": "节约额度",
                                "normal": "可尝试继续；额度待确认"
                                if data["stale"]
                                or (data.get("policy") or {}).get("posture")
                                == "cooldown"
                                else "额度可用",
                                "unknown": "待确认",
                            }.get(posture, posture)
                        )
                    if recovery:
                        print("    自动续跑：" + recovery["detail"])
                    elif explicit_hold and watches is not None:
                        print(
                            "    自动续跑：保留人工或原生会话设置的冷却；不自动覆盖。"
                        )
                    eligible = (
                        not recovery
                        or recovery["state"] not in {"paused", "explicit_hold"}
                    ) and not explicit_hold
                    if watches is not None:
                        if watches["recovery"] is None or watches["inbox"] is None:
                            print(
                                "    监听任务：未知（当前环境无法检查本机进程；不推断为 0）"
                            )
                            if (
                                eligible
                                and recovery
                                and recovery["state"] == "unconfirmed"
                            ):
                                print(
                                    "    最小恢复动作：恢复通知尚无原生回执；请在该原 agy 窗口发送一条简短消息，令其同步待办并重新挂载监听。"
                                )
                        else:
                            print(
                                f"    监听任务：恢复等待 {watches['recovery']} · 收件箱等待 {watches['inbox']}"
                            )
                            if eligible:
                                if (
                                    recovery and recovery["state"] == "unconfirmed"
                                ) or (
                                    watches["recovery"] == 0 and watches["inbox"] == 0
                                ):
                                    print(
                                        "    最小恢复动作：自动续跑无法外部拉起已停止的 agy 回合；请在该原 agy 窗口发送一条简短消息，令其同步待办并重新挂载 --mode recovery 与空闲 --mode actionable 监听。"
                                    )
                                elif watches["recovery"] == 0:
                                    print(
                                        "    提示：缺少 --mode recovery 持续监听；请在该原 agy 窗口补挂，避免后续额度耗尽后无恢复等待。"
                                    )
                                elif watches["inbox"] == 0:
                                    print(
                                        "    提示：当前无空闲收件箱监听（若会话正在执行回合则属正常；若已空闲则需在原窗口补挂 --mode actionable）。"
                                    )
                    elif eligible and recovery and recovery["state"] == "unconfirmed":
                        print(
                            "    最小恢复动作：恢复通知尚无原生回执；请在该原 agy 窗口发送一条简短消息，令其同步待办并重新挂载监听。"
                        )
                    if not data:
                        continue
                    if member["provider"] == "claude":
                        _print_claude_windows(data["windows"])
                        continue
                    for window in data["windows"]:
                        if not window["applies"]:
                            continue
                        remaining = (
                            "待刷新"
                            if window["stale"]
                            else "未知"
                            if window.get("remaining") is None
                            else f"{window['remaining']:g}%"
                        )
                        print(f"    {window['bucket']}: {remaining}")
                print(
                    "原生快照不预留额度；同一账户的数字不能相加。到达重置时间后仍需验证恢复。"
                )
        elif args.command == "availability":
            from .availability import (
                Availability,
                describe_availability,
                describe_observations,
                read_availability,
            )
            from .inspection import readonly_snapshot

            if not args.availability_status and (
                args.reason
                or args.resume_after is not None
                or args.observations_json is not None
            ):
                parser.error(
                    "--reason/--resume-after/--observations-json require --set"
                )
            project = str(args.project.expanduser().resolve())
            with readonly_snapshot(args.state) as db:
                member = (
                    db.execute(
                        "SELECT id FROM members WHERE project=? AND alias=?",
                        (project, args.alias),
                    ).fetchone()
                    if db
                    else None
                )
                if member is None:
                    raise TeamError(
                        "Availability requires this project's already-enrolled alias"
                    )
                actor = member["id"]
                value = read_availability(db, actor)
            if args.availability_status:
                service = Availability(Store(args.state))
                extended = (
                    args.availability_status in {"unknown", "normal", "conserve"}
                    or args.observations_json is not None
                )
                extra = {}
                if extended:
                    try:
                        extra["observations"] = (
                            json.loads(args.observations_json)
                            if args.observations_json is not None
                            else None
                        )
                    except json.JSONDecodeError as error:
                        raise TeamError("Observations must be valid JSON") from error
                setter = service.record if extended else service.set
                status = (
                    "normal"
                    if extended and args.availability_status == "ready"
                    else args.availability_status
                )
                value = setter(
                    actor,
                    value["revision"],
                    status,
                    args.reason,
                    args.resume_after,
                    recorded_by="local operator",
                    **extra,
                )
            if args.json:
                print(json.dumps(value, ensure_ascii=False))
            else:
                print(f"{args.alias}: {describe_availability(value)}")
                if value["reason"]:
                    print(f"  Recorded reason: {value['reason']}")
                for line in describe_observations(value):
                    print(f"  Observation: {line}")
                print("Recorded availability is not a live quota or online check.")
        elif args.command == "ui":
            from .workbench import serve

            if not 0 <= args.port <= 65535:
                parser.error("--port must be between 0 and 65535")
            serve(args.state, args.port, not args.no_open)
        elif args.command == "teams":
            from .inspection import list_teams

            report = list_teams(args.state, args.project)
            if args.json:
                print(json.dumps(report, ensure_ascii=False, indent=2))
                return
            scope = (
                str(args.project.expanduser().resolve())
                if args.project
                else "All projects"
            )
            print(f"Native Team · {scope} · {len(report)} teams")
            for team in report:
                print(f"\n{team['name']}  [{team['state']}]")
                print(f"  Project: {team['project']}")
                members = ", ".join(
                    member["alias"]
                    + (f" ({member['state']})" if member["state"] != "active" else "")
                    for member in team["members"]
                )
                print(f"  Members ({len(team['members'])}): {members or 'none'}")
            if not report:
                print("No teams registered in this scope.")
            else:
                print(
                    "\nTeam status does not indicate whether each terminal is online."
                )
        elif args.command == "migrate":
            from .schema import migrate

            backup = migrate(Store(args.state))
            print(
                f"Native Team ledger ready; backup: {backup}"
                if backup
                else "Native Team ledger already current; no changes"
            )
        elif args.command == "monitor":
            from .monitor import serve

            serve(args.state, args.codex)
        elif args.command == "mcp":
            from .mcp_server import serve

            serve(args.state, args.client)
        else:
            if not (args.state / "team.sqlite3").is_file():
                print("Native Team: no participants have joined yet")
                return
            store = Store(args.state)
            report = store.inspect(args.project)
            from .monitor import running
            from .peers import PeerStore

            report["monitor_active"] = running(args.state)
            report["teams"] = PeerStore(store).inspect(args.project)
            if args.json:
                print(json.dumps(report, ensure_ascii=False, indent=2))
                return
            print(f"Native Team · {report['project']}")
            print(
                "  Notification monitor: "
                + ("active" if report["monitor_active"] else "not running")
            )
            for member in report["members"]:
                age = max(0, int(time.time() - member["last_seen"]))
                print(
                    f"  {member['alias']} [{member['provider']}]: last native tool contact {age}s ago"
                )
            for team in report["teams"]:
                print(f"  Team {team['name']}: {team['state']}")
                for member in team["members"]:
                    print(
                        f"    {member['alias']} [{member['provider']}, {member['state']}]: {member['lane'] or 'workstream not set'}"
                    )
                    from .availability import (
                        describe_availability,
                        describe_observations,
                    )

                    if member.get("availability", {}).get("recorded"):
                        print(
                            f"      Availability: {describe_availability(member['availability'])}"
                        )
                        for line in describe_observations(member["availability"]):
                            print(f"      Observation: {line}")
                    waiting = member.get("waiting", {})
                    if waiting.get("unreceived_messages"):
                        print(
                            f"      {waiting['unreceived_messages']} unread important messages; oldest {int(waiting['message_age_seconds'])}s"
                        )
                    if waiting.get("pending_tasks"):
                        print(
                            f"      {waiting['pending_tasks']} tasks awaiting claim/review; oldest {int(waiting['task_age_seconds'])}s (reading is not completion)"
                        )
                print(f"    Tasks: {team['tasks'] or 'none'}")
                if team["delivery_attention"]:
                    print(
                        f"    Attention: {team['delivery_attention']} deliveries need native receipt inspection"
                    )
            for lead in report["coordinators"]:
                state = lead["status"]
                print(f"  {lead['alias']}: {state['action']}")
                print(
                    f"    {len(state['work'])} work items; {sum(t['state'] == 'accepted' for t in state['assignments'])}/{len(state['assignments'])} assignments accepted"
                )
                for notice in state["notifications"]:
                    if notice["received_at"] is None and notice["state"] in {
                        "failed",
                        "uncertain",
                        "queued_warning",
                    }:
                        print(
                            f"    Attention: notification {notice['state']}; native receipt unconfirmed"
                        )
                for work in state["work"]:
                    if (
                        work["state"] == "active"
                        and work["reminders"] >= 2
                        and time.time() - work["updated"] > 300
                    ):
                        print(
                            "    Attention: continuation reminders exhausted; inspect the native coordinator"
                        )
            print(
                "Tool contact is not proof of an active native schedule. Verify actual task transitions."
            )
    except (TeamError, OSError) as error:
        print(f"Native Team: {error}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
