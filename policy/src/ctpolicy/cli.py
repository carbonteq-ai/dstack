"""`dstack-quota`: read-only view of policy state.

Deliberately its own binary. dstack's CLI registers its commands from a
hard-coded list in `cli/main.py` with no entry-point mechanism, so a subcommand
cannot be added without forking it — and the design is to leave the official CLI
alone.

It reads the same two files the server does: policy.yaml for the limits and the
usage snapshot for what has been consumed. That means it runs where those files
are — on the server host, typically as

    docker compose exec server dstack-quota

Nothing here talks to the dstack API, so it cannot be wrong about what the
plugin will decide: it is reading exactly the inputs the plugin reads. The
user-facing surface for people on laptops remains the rejection message, which
already states the limit, what is used, and what is committed.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from ctpolicy import config as policy_config
from ctpolicy import usage as usage_module
from ctpolicy import windows
from ctpolicy.config import PolicyConfig, PolicySpec, TeamConfig
from ctpolicy.usage import Snapshot, SnapshotUnavailable


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dstack-quota",
        description="Show CarbonTeq team and user policy state (read-only).",
    )
    parser.add_argument("--team", help="Only show this team")
    parser.add_argument("--user", help="Only show this user")
    parser.add_argument("--policy", type=Path, help="Path to policy.yaml")
    parser.add_argument("--snapshot", type=Path, help="Path to the usage snapshot")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a table")
    args = parser.parse_args(argv)

    try:
        config = policy_config.load(args.policy)
    except ValueError as e:
        print(f"dstack-quota: {e}", file=sys.stderr)
        return 2

    now = datetime.now(timezone.utc)
    snapshot: Optional[Snapshot] = None
    snapshot_error: Optional[str] = None
    try:
        snapshot = usage_module.load(args.snapshot, max_age=None)
    except SnapshotUnavailable as e:
        snapshot_error = str(e)

    teams = _selected_teams(config, args.team)
    if not teams:
        print(f"dstack-quota: no team named {args.team!r} in policy.yaml", file=sys.stderr)
        return 2

    if args.json:
        print(
            json.dumps(_as_json(config, teams, snapshot, snapshot_error, now, args.user), indent=2)
        )
        return 0

    _print_report(config, teams, snapshot, snapshot_error, now, args.user)
    return 0


def _selected_teams(config: PolicyConfig, team: Optional[str]) -> List[str]:
    if team is None:
        return sorted(config.teams)
    return [team] if team in config.teams else []


def _print_report(
    config: PolicyConfig,
    teams: List[str],
    snapshot: Optional[Snapshot],
    snapshot_error: Optional[str],
    now: datetime,
    only_user: Optional[str],
) -> None:
    if snapshot_error:
        # Say this once, loudly, rather than printing "0h used" everywhere and
        # letting someone believe it.
        print(f"! usage unavailable ({snapshot_error})")
        print(
            f"! budget figures below are unknown, not zero."
            f" on_usage_unavailable = {config.on_usage_unavailable}\n"
        )
    elif snapshot is not None:
        age = int(snapshot.age(now).total_seconds())
        print(f"usage as of {snapshot.generated_at:%Y-%m-%d %H:%M:%S %Z} ({age}s ago)\n")

    for team in teams:
        team_config = config.teams[team]
        low, high = config.band(team)
        print(f"{team}   priority band {low}-{high}")
        print(f"  window  {_window_line(team_config.defaults, config, now)}")
        print(f"  {_limits_line(team_config.defaults, config, snapshot, team, None, now)}")

        users = sorted(team_config.users)
        if only_user is not None:
            users = [u for u in users if u == only_user]
        for user in users:
            spec = team_config.users[user]
            line = _limits_line(spec, config, snapshot, team, user, now, user_scope=True)
            print(f"  {user:<20} {line}")
        print()


def _window_line(spec: PolicySpec, config: PolicyConfig, now: datetime) -> str:
    if spec.windows is None:
        return "always open"
    intervals = windows.materialize(spec.windows, config.tz, now)
    allowed = "; ".join(w.pretty() for w in spec.windows)
    if windows.is_open(intervals, now):
        close = windows.current_close(intervals, now)
        left = usage_module.format_duration((close - now).total_seconds()) if close else "?"
        return f"{allowed} {config.timezone} — open, closes in {left}"
    opens = windows.next_open(intervals, now)
    when = f", opens {opens:%a %H:%M}" if opens else ""
    return f"{allowed} {config.timezone} — shut{when}"


def _limits_line(
    spec: PolicySpec,
    config: PolicyConfig,
    snapshot: Optional[Snapshot],
    team: str,
    user: Optional[str],
    now: datetime,
    user_scope: bool = False,
) -> str:
    parts: List[str] = []
    scope = snapshot.scope(team, user) if snapshot is not None else None

    if spec.max_run_duration is not None:
        parts.append(f"max run {usage_module.format_duration(spec.max_run_duration)}")

    if spec.time_budget is not None:
        limit = spec.time_budget.limit
        if scope is None:
            parts.append(f"time ?/{usage_module.format_duration(limit)}")
        else:
            used = scope.usage_for(spec.time_budget.period)
            parts.append(
                f"time {usage_module.format_duration(used.seconds)}"
                f"/{usage_module.format_duration(limit)}"
                f" (+{usage_module.format_duration(scope.committed.seconds)} committed)"
            )

    cloud = spec.cloud
    if cloud is None or not cloud.allowed:
        if not user_scope or cloud is not None:
            parts.append("cloud off")
    else:
        if cloud.dollar_budget is not None:
            limit = cloud.dollar_budget.limit
            if scope is None:
                parts.append(f"cloud $?/{limit:.0f}")
            else:
                used = scope.usage_for(cloud.dollar_budget.period)
                parts.append(
                    f"cloud ${used.dollars:.2f}/${limit:.2f}"
                    f" (+${scope.committed.dollars:.2f} committed)"
                )
        else:
            parts.append("cloud on, no budget")
        if cloud.max_price is not None:
            parts.append(f"max ${cloud.max_price:.2f}/hr")

    return "  ".join(parts) if parts else "inherits team defaults"


def _as_json(
    config: PolicyConfig,
    teams: List[str],
    snapshot: Optional[Snapshot],
    snapshot_error: Optional[str],
    now: datetime,
    only_user: Optional[str],
) -> dict:
    out: dict = {
        "generated_at": snapshot.generated_at.isoformat() if snapshot else None,
        "usage_available": snapshot is not None,
        "teams": {},
    }
    if snapshot_error:
        out["usage_error"] = snapshot_error
    for team in teams:
        team_config: TeamConfig = config.teams[team]
        low, high = config.band(team)
        entry = {
            "band": [low, high],
            "window_open": _is_open(team_config.defaults, config, now),
            "team": _scope_json(team_config.defaults, snapshot, team, None),
            "users": {},
        }
        for user, spec in sorted(team_config.users.items()):
            if only_user is not None and user != only_user:
                continue
            entry["users"][user] = _scope_json(spec, snapshot, team, user)
        out["teams"][team] = entry
    return out


def _is_open(spec: PolicySpec, config: PolicyConfig, now: datetime) -> bool:
    if spec.windows is None:
        return True
    return windows.is_open(windows.materialize(spec.windows, config.tz, now), now)


def _scope_json(
    spec: PolicySpec, snapshot: Optional[Snapshot], team: str, user: Optional[str]
) -> dict:
    scope = snapshot.scope(team, user) if snapshot is not None else None
    entry: dict = {}
    if spec.time_budget is not None:
        used = scope.usage_for(spec.time_budget.period).seconds if scope else None
        entry["time_budget"] = {
            "period": spec.time_budget.period.value,
            "limit_seconds": spec.time_budget.limit,
            "used_seconds": used,
            "committed_seconds": scope.committed.seconds if scope else None,
        }
    cloud = spec.cloud
    if cloud is not None and cloud.dollar_budget is not None:
        used = scope.usage_for(cloud.dollar_budget.period).dollars if scope else None
        entry["dollar_budget"] = {
            "period": cloud.dollar_budget.period.value,
            "limit": cloud.dollar_budget.limit,
            "used": used,
            "committed": scope.committed.dollars if scope else None,
        }
    if cloud is not None:
        entry["cloud_allowed"] = cloud.allowed
    return entry


if __name__ == "__main__":
    sys.exit(main())
