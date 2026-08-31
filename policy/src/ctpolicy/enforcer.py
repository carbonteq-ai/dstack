"""The policy enforcer: refreshes the usage snapshot and stops breaching runs.

Runs as its own process because dstack has no plugin hook for background work —
`server/background/` is a hard-coded list of pipelines and scheduled tasks, with
no entry-point mechanism. It talks to the server over the public HTTP API with a
global-admin token, which sees every project.

Two jobs, once per interval:

1. Recompute usage from dstack's own records and write the snapshot the apply
   hook reads. This is what makes budgets enforceable at admission.
2. Stop runs that have outlived their policy. Admission already bounds a run's
   worst case by clamping `max_duration`, which the runner enforces inside the
   VM, so this is a backstop for the cases a clamp cannot cover: a run
   overshooting its window by its provisioning time, a policy edited while runs
   are in flight, and prices that moved after admission.

It only ever stops runs for the three reasons the design allows — outside the
compute window, past the duration ceiling, or over budget. It never stops a run
because something else wants its capacity.
"""

import logging
import os
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple

from dstack.api.server import APIClient

from ctpolicy import config as policy_config
from ctpolicy import usage as usage_module
from ctpolicy import windows
from ctpolicy._compat import ClientError
from ctpolicy.config import PolicyConfig, PolicySpec, TeamConfig
from ctpolicy.usage import Snapshot

logger = logging.getLogger("ctpolicy.enforcer")

DEFAULT_INTERVAL_SECONDS = 60
PAGE_SIZE = 100

PROVISIONING_GRACE_SECONDS = 30 * 60
"""Slack allowed before the duration backstop fires.

The runner's `max_duration` clock starts when a job starts *running*, but all the
enforcer can see is `submitted_at`. Without slack, a run that sat in the queue
would be stopped for time it never spent computing. The runner enforces the real
bound, so this only needs to be generous enough to avoid false positives while
still catching a policy that was tightened mid-run.
"""


@dataclass
class Breach:
    run_name: str
    project: str
    user: str
    reason: str


class _Stopping:
    """Tracks whether a shutdown signal has arrived."""

    def __init__(self) -> None:
        self.requested = False

    def request(self, *_args) -> None:
        logger.info("Shutdown requested; finishing the current cycle")
        self.requested = True


def main() -> int:
    logging.basicConfig(
        level=os.getenv("DSTACK_CT_ENFORCER_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    server_url = os.getenv("DSTACK_SERVER_URL")
    token = os.getenv("DSTACK_SERVER_ADMIN_TOKEN")
    if not server_url or not token:
        logger.error("DSTACK_SERVER_URL and DSTACK_SERVER_ADMIN_TOKEN must both be set")
        return 2

    interval = int(os.getenv("DSTACK_CT_ENFORCER_INTERVAL", DEFAULT_INTERVAL_SECONDS))
    dry_run = _env_flag("DSTACK_CT_ENFORCER_DRY_RUN")
    if dry_run:
        logger.warning(
            "DRY RUN: usage snapshots are written, but no run will be stopped."
            " Unset DSTACK_CT_ENFORCER_DRY_RUN to enforce."
        )

    client = APIClient(base_url=server_url, token=token)
    stopping = _Stopping()
    signal.signal(signal.SIGTERM, stopping.request)
    signal.signal(signal.SIGINT, stopping.request)

    logger.info(
        "Enforcer started; interval %ss, snapshot %s", interval, usage_module.snapshot_file_path()
    )
    while not stopping.requested:
        started = time.monotonic()
        try:
            run_cycle(client, dry_run=dry_run)
        except ClientError as e:
            # The server restarting is routine, not a defect. Say so in one line
            # rather than a traceback that trains people to ignore the logs.
            logger.warning("Server unreachable (%s); retrying next interval", e)
        except Exception:
            # Any other error must not end the loop either: the snapshot going
            # stale is what makes budgeted teams fail closed.
            logger.exception("Enforcer cycle failed; retrying next interval")
        elapsed = time.monotonic() - started
        _sleep_until(interval - elapsed, stopping)
    logger.info("Enforcer stopped")
    return 0


def run_cycle(client: APIClient, dry_run: bool = False) -> Snapshot:
    config = policy_config.load()
    now = datetime.now(timezone.utc)

    runs = fetch_runs(client, config, now)
    snapshot = usage_module.build(runs, config.teams.keys(), config.tz, now)
    usage_module.write(snapshot)
    logger.info(
        "Snapshot written: %d teams, %d runs considered%s",
        len(snapshot.teams),
        len(runs),
        f", {snapshot.unbounded_runs} active job(s) with no max_duration"
        if snapshot.unbounded_runs
        else "",
    )

    breaches = find_breaches(runs, config, snapshot, now)
    for breach in breaches:
        if dry_run:
            logger.warning(
                "WOULD STOP %s/%s (%s): %s",
                breach.project,
                breach.run_name,
                breach.user,
                breach.reason,
            )
            continue
        logger.warning(
            "Stopping %s/%s (%s): %s", breach.project, breach.run_name, breach.user, breach.reason
        )
        try:
            # Graceful, so the run's `stop_duration` is honoured and the
            # workload gets its shutdown window.
            client.runs.stop(breach.project, [breach.run_name], abort=False)
        except Exception:
            logger.exception("Failed to stop %s/%s", breach.project, breach.run_name)
    return snapshot


def fetch_runs(client: APIClient, config: PolicyConfig, now: datetime) -> List:
    """Every run that can affect a decision: all active ones, plus this period's.

    Two queries rather than one. Paginating the full history until it predates
    the period would still miss a long-running job submitted before the period
    began, and listing only active runs would miss everything already finished
    that has spent budget.
    """
    earliest = min(
        usage_module.period_start(period, now, config.tz) for period in usage_module.Period
    )
    by_id: Dict = {}
    for run in _paginate(client, only_active=True, stop_before=None):
        by_id[run.id] = run
    for run in _paginate(client, only_active=False, stop_before=earliest):
        by_id.setdefault(run.id, run)
    return list(by_id.values())


def _paginate(client: APIClient, only_active: bool, stop_before: Optional[datetime]) -> Iterable:
    prev_submitted_at: Optional[datetime] = None
    prev_run_id = None
    while True:
        page = client.runs.list(
            project_name=None,  # a global admin sees every project
            repo_id=None,
            only_active=only_active,
            prev_submitted_at=prev_submitted_at,
            prev_run_id=prev_run_id,
            limit=PAGE_SIZE,
            ascending=False,
            include_jobs=True,
            # Cost is summed over job submissions, so a limit here would
            # under-count it. None means the server's maximum.
            job_submissions_limit=None,
        )
        if not page:
            return
        for run in page:
            yield run
        last = page[-1]
        if (
            stop_before is not None
            and last.submitted_at.replace(tzinfo=last.submitted_at.tzinfo or timezone.utc)
            < stop_before
        ):
            return
        if len(page) < PAGE_SIZE:
            return
        prev_submitted_at, prev_run_id = last.submitted_at, last.id


def find_breaches(
    runs: Iterable, config: PolicyConfig, snapshot: Snapshot, now: datetime
) -> List[Breach]:
    breaches: List[Breach] = []
    for run in runs:
        if _is_finished(run):
            continue
        project = getattr(run, "project_name", None)
        user = getattr(run, "user", None)
        if project in config.ungoverned_projects or project not in config.teams:
            continue
        team_config = config.teams[project]
        try:
            policy = policy_config.resolve(config, project, user)
        except ValueError:
            # The user was removed from policy while their run was going. That
            # is not one of the three reasons a run may be stopped, so leave it
            # alone and say so.
            logger.warning(
                "Run %s/%s belongs to %s, who has no policy entry; leaving it running",
                project,
                _run_name(run),
                user,
            )
            continue
        reason = _breach_reason(run, config, team_config, policy, snapshot, now, project, user)
        if reason is not None:
            name = _run_name(run)
            if name is None:
                logger.warning("Run %s in %s has no name; cannot stop it", run.id, project)
                continue
            breaches.append(
                Breach(run_name=name, project=project, user=user or "?", reason=reason)
            )
    return breaches


def _run_name(run) -> Optional[str]:
    """The name `runs.stop` takes.

    `Run` itself has no `run_name`; it lives on the spec. Reading it off the run
    would raise on every breach, which is precisely when it matters.
    """
    spec = getattr(run, "run_spec", None)
    return getattr(spec, "run_name", None) if spec is not None else None


def _breach_reason(
    run,
    config: PolicyConfig,
    team_config: TeamConfig,
    policy: PolicySpec,
    snapshot: Snapshot,
    now: datetime,
    project: str,
    user: Optional[str],
) -> Optional[str]:
    if policy.windows is not None:
        intervals = windows.materialize(policy.windows, config.tz, now)
        if not windows.is_open(intervals, now):
            allowed = "; ".join(w.pretty() for w in policy.windows)
            return f"outside the {project} compute window ({allowed} {config.timezone})"

    if policy.max_run_duration is not None:
        elapsed = (now - _as_aware(run.submitted_at)).total_seconds()
        if elapsed > policy.max_run_duration + PROVISIONING_GRACE_SECONDS:
            return (
                f"running {usage_module.format_duration(elapsed)}, past the"
                f" {usage_module.format_duration(policy.max_run_duration)} ceiling"
            )

    over = _over_budget(team_config, snapshot, project, user, now, config)
    if over is not None:
        return over
    return None


def _over_budget(
    team_config: TeamConfig,
    snapshot: Snapshot,
    project: str,
    user: Optional[str],
    now: datetime,
    config: PolicyConfig,
) -> Optional[str]:
    """Whether an applicable budget is already spent.

    Compares against `spent` alone, never `committed`: committed is the run's own
    remaining entitlement, so including it would stop every run the moment it
    started.
    """
    scopes: List[Tuple[str, PolicySpec, Optional[str]]] = [
        (f"team {project!r}", team_config.defaults, None),
    ]
    if user is not None and user in team_config.users:
        scopes.append((f"user {user!r}", team_config.users[user], user))

    for label, spec, scope_user in scopes:
        scope = snapshot.scope(project, scope_user)
        if spec.time_budget is not None:
            used = scope.usage_for(spec.time_budget.period).seconds
            if used >= spec.time_budget.limit:
                return (
                    f"{label} is over its time budget for"
                    f" {usage_module.period_label(spec.time_budget.period, now, config.tz)}:"
                    f" {usage_module.format_duration(used)} of"
                    f" {usage_module.format_duration(spec.time_budget.limit)}"
                )
        cloud = spec.cloud
        if cloud is not None and cloud.dollar_budget is not None:
            used = scope.usage_for(cloud.dollar_budget.period).dollars
            if used >= cloud.dollar_budget.limit:
                return (
                    f"{label} is over its cloud budget for"
                    f" {usage_module.period_label(cloud.dollar_budget.period, now, config.tz)}:"
                    f" ${used:.2f} of ${cloud.dollar_budget.limit:.2f}"
                )
    return None


def _is_finished(run) -> bool:
    status = getattr(run, "status", None)
    return status is not None and status.is_finished()


def _as_aware(value: datetime) -> datetime:
    """dstack serializes naive UTC timestamps in places; treat them as UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


def _sleep_until(seconds: float, stopping: _Stopping) -> None:
    """Sleep in short slices so a signal is noticed promptly."""
    deadline = time.monotonic() + max(0.0, seconds)
    while not stopping.requested and time.monotonic() < deadline:
        time.sleep(min(1.0, deadline - time.monotonic()))


if __name__ == "__main__":
    sys.exit(main())
