"""Usage accounting: the snapshot the plugin reads and the enforcer writes.

Admission has to know how much of a budget is already gone, but the apply hook
cannot ask the database. It runs on a worker thread of the server's shared
executor while the request still holds a DB session, and dstack's own db.py
documents that cross-thread access needs a whole new engine rather than the
shared pool. So the enforcer recomputes usage on its own schedule and leaves a
small JSON file behind, and the hook does a stat plus a parse.

The file is a cache, never an authority: every value in it is derived from
dstack's own records and can be recomputed from scratch at any time. Nothing
this package owns is load-bearing for a decision. Staleness is bounded by
`usage_snapshot_max_age`, past which a budgeted policy fails closed.

Two accounting choices worth knowing:

* Time is measured from a job submission's `submitted_at` to its `finished_at`,
  which is how dstack computes `Run.cost`. Keeping the two consistent matters
  more than excluding queue time, and it errs high — a run that waited is
  charged for waiting. `max_duration`, which is what bounds a run, measures only
  running time, so the budget is conservative relative to the clamp rather than
  the other way round.
* A run occupying several nodes or replicas accrues once per job submission, so
  a two-node run consumes twice the wall-clock. That is the resource it holds.
"""

import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from ctpolicy._compat import BackendType
from ctpolicy.config import Period

DEFAULT_SNAPSHOT_FILE = "/var/lib/ctpolicy/usage-snapshot.json"
SNAPSHOT_FILE_ENV_VAR = "DSTACK_CT_USAGE_SNAPSHOT"

SNAPSHOT_VERSION = 1


class SnapshotUnavailable(Exception):
    """The snapshot is missing, unreadable, stale, or the wrong shape."""


def snapshot_file_path() -> Path:
    return Path(os.getenv(SNAPSHOT_FILE_ENV_VAR, DEFAULT_SNAPSHOT_FILE))


class Amounts(BaseModel):
    seconds: float = 0.0
    dollars: float = 0.0

    def __add__(self, other: "Amounts") -> "Amounts":
        return Amounts(seconds=self.seconds + other.seconds, dollars=self.dollars + other.dollars)


class ScopeUsage(BaseModel):
    """Usage for one team, or one user within a team."""

    spent: Dict[Period, Amounts] = {}
    """Keyed by period so a team budget and a user budget can differ in period
    without the enforcer needing to know which each one picked."""
    committed: Amounts = Amounts()
    """Worst-case remaining exposure of runs that are still active: what they
    are still entitled to consume under the ceilings they were admitted with.

    Admission subtracts this as well as `spent`. Without it, several runs each
    individually under budget can collectively exceed it.
    """

    def usage_for(self, period: Period) -> Amounts:
        return self.spent.get(period, Amounts())


class TeamUsage(BaseModel):
    team: ScopeUsage = ScopeUsage()
    users: Dict[str, ScopeUsage] = {}


class Snapshot(BaseModel):
    version: int = SNAPSHOT_VERSION
    generated_at: datetime
    teams: Dict[str, TeamUsage] = {}
    unbounded_runs: int = 0
    """Active runs with no `max_duration`, which contribute nothing to
    `committed` because their exposure cannot be bounded. Only reachable for
    runs admitted before a budget existed; the enforcer logs them."""

    def scope(self, team: str, user: Optional[str] = None) -> ScopeUsage:
        team_usage = self.teams.get(team)
        if team_usage is None:
            return ScopeUsage()
        if user is None:
            return team_usage.team
        return team_usage.users.get(user, ScopeUsage())

    def age(self, now: datetime) -> timedelta:
        return now - self.generated_at


def period_start(period: Period, now: datetime, tz: ZoneInfo) -> datetime:
    """The instant the current budget period began, in the config timezone."""
    local = now.astimezone(tz)
    if period == Period.MONTH:
        start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        start = midnight - timedelta(days=local.weekday())  # ISO week, Monday
    return start


def load(
    path: Optional[Path] = None, max_age: Optional[float] = None, now: Optional[datetime] = None
) -> Snapshot:
    """Read the snapshot, raising `SnapshotUnavailable` rather than guessing.

    Every failure mode collapses to one exception because the caller's response
    is the same for all of them: apply `on_usage_unavailable`.
    """
    path = path or snapshot_file_path()
    try:
        raw = path.read_text()
    except OSError as e:
        raise SnapshotUnavailable(f"cannot read {path}: {e}")
    try:
        snapshot = Snapshot.parse_raw(raw)
    except Exception as e:
        raise SnapshotUnavailable(f"{path} is not a usable snapshot: {e}")
    if snapshot.version != SNAPSHOT_VERSION:
        raise SnapshotUnavailable(
            f"{path} is version {snapshot.version}, this build reads {SNAPSHOT_VERSION}"
        )
    if max_age is not None:
        now = now or datetime.now(snapshot.generated_at.tzinfo)
        age = snapshot.age(now).total_seconds()
        if age > max_age:
            raise SnapshotUnavailable(f"{path} is {int(age)}s old, past the {int(max_age)}s limit")
    return snapshot


def write(snapshot: Snapshot, path: Optional[Path] = None) -> None:
    """Write the snapshot atomically.

    The plugin reads this file on unrelated threads while the enforcer rewrites
    it, so it is written to a sibling temporary file and renamed. A half-written
    file would parse as garbage and fail every budgeted admission.
    """
    path = path or snapshot_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".usage-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(snapshot.json())
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with_suppressed_error(tmp_name)
        raise


def with_suppressed_error(name: str) -> None:
    try:
        os.unlink(name)
    except OSError:
        pass


# --- computing the snapshot from dstack's own records -----------------------


def _submission_window(submission, now: datetime) -> Optional[Tuple[datetime, datetime]]:
    started = getattr(submission, "submitted_at", None)
    if started is None:
        return None
    finished = getattr(submission, "finished_at", None) or now
    if finished < started:
        return None
    return started, finished


def _overlap_seconds(start: datetime, end: datetime, window_start: datetime) -> float:
    """Seconds of [start, end) that fall on or after `window_start`.

    Budgets are per period, so a run straddling a period boundary must only
    charge the part inside the current one.
    """
    effective = max(start, window_start)
    if end <= effective:
        return 0.0
    return (end - effective).total_seconds()


def _is_on_prem(submission) -> Optional[bool]:
    """Whether this submission landed on the SSH fleet, or None if not yet placed."""
    jpd = getattr(submission, "job_provisioning_data", None)
    if jpd is None:
        return None
    backend = getattr(jpd, "backend", None)
    if backend is None:
        return None
    return backend == BackendType.REMOTE


def _run_max_duration(run) -> Optional[int]:
    spec = getattr(run, "run_spec", None)
    profile = getattr(spec, "merged_profile", None) if spec is not None else None
    value = getattr(profile, "max_duration", None)
    if value is None and spec is not None:
        value = getattr(getattr(spec, "configuration", None), "max_duration", None)
    return value if isinstance(value, int) else None


def _run_max_price(run) -> Optional[float]:
    spec = getattr(run, "run_spec", None)
    profile = getattr(spec, "merged_profile", None) if spec is not None else None
    value = getattr(profile, "max_price", None)
    if value is None and spec is not None:
        value = getattr(getattr(spec, "configuration", None), "max_price", None)
    return value


def _pinned_on_prem(run) -> bool:
    """Whether the stored spec can only ever place on the SSH fleet.

    The plugin pins `backends` to `[remote]` for teams without cloud, so a run
    that carries that pin cannot accrue dollars however long it runs.
    """
    spec = getattr(run, "run_spec", None)
    profile = getattr(spec, "merged_profile", None) if spec is not None else None
    backends = getattr(profile, "backends", None)
    if backends is None and spec is not None:
        backends = getattr(getattr(spec, "configuration", None), "backends", None)
    if not backends:
        return False
    return all(b == BackendType.REMOTE for b in backends)


def accrue(run, now: datetime, window_start: datetime) -> Tuple[Amounts, Amounts, int]:
    """Return (spent, committed, unbounded_jobs) for one run within a period.

    `spent` is what the run has already consumed inside the period. `committed`
    is what it is still entitled to consume, derived from the `max_duration` it
    was admitted with — which the runner enforces in the VM, so it is a real
    bound rather than an estimate.
    """
    spent = Amounts()
    committed = Amounts()
    unbounded = 0

    max_duration = _run_max_duration(run)
    max_price = _run_max_price(run)
    pinned_on_prem = _pinned_on_prem(run)

    # dstack computes this the same way, summing price x duration over the same
    # submissions; reusing it keeps our dollars identical to what `dstack ps`
    # and the API report.
    total_cost = float(getattr(run, "cost", 0.0) or 0.0)
    total_seconds = 0.0
    period_seconds = 0.0

    for job in getattr(run, "jobs", []) or []:
        for submission in getattr(job, "job_submissions", []) or []:
            window = _submission_window(submission, now)
            if window is None:
                continue
            started, finished = window
            total_seconds += (finished - started).total_seconds()
            in_period = _overlap_seconds(started, finished, window_start)
            period_seconds += in_period

            status = getattr(submission, "status", None)
            active = status is not None and not status.is_finished()
            if not active:
                continue
            if max_duration is None:
                unbounded += 1
                continue
            elapsed = (finished - started).total_seconds()
            remaining = max(0.0, max_duration - elapsed)
            committed.seconds += remaining
            if not pinned_on_prem and _is_on_prem(submission) is not True and max_price:
                committed.dollars += max_price * remaining / 3600.0

    spent.seconds = period_seconds
    # Cost is only available for the run as a whole, so attribute it to the
    # period in proportion to the time that fell inside it. Exact when a run
    # does not straddle a boundary, which is the normal case.
    if total_seconds > 0:
        spent.dollars = total_cost * (period_seconds / total_seconds)
    else:
        spent.dollars = 0.0
    return spent, committed, unbounded


def build(runs: Iterable, teams: Iterable[str], tz: ZoneInfo, now: datetime) -> Snapshot:
    """Aggregate runs into a snapshot, for every team and both periods."""
    team_set = set(teams)
    starts = {period: period_start(period, now, tz) for period in Period}
    snapshot = Snapshot(generated_at=now, teams={team: TeamUsage() for team in team_set})

    for run in runs:
        team = getattr(run, "project_name", None)
        if team not in team_set:
            continue  # ungoverned project, or a project with no policy
        user = getattr(run, "user", None)
        team_usage = snapshot.teams[team]
        user_usage = team_usage.users.setdefault(user, ScopeUsage()) if user else None

        committed_counted = False
        for period, start in starts.items():
            spent, committed, unbounded = accrue(run, now, start)
            _add(team_usage.team.spent, period, spent)
            if user_usage is not None:
                _add(user_usage.spent, period, spent)
            # `committed` does not depend on the period; count it once.
            if not committed_counted:
                team_usage.team.committed = team_usage.team.committed + committed
                if user_usage is not None:
                    user_usage.committed = user_usage.committed + committed
                snapshot.unbounded_runs += unbounded
                committed_counted = True
    return snapshot


def _add(bucket: Dict[Period, Amounts], period: Period, amounts: Amounts) -> None:
    bucket[period] = bucket.get(period, Amounts()) + amounts


def format_duration(seconds: float) -> str:
    """Compact hours-and-minutes, for messages people read."""
    total_minutes = int(seconds // 60)
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        return f"{hours}h{minutes:02d}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def period_label(period: Period, now: datetime, tz: ZoneInfo) -> str:
    start = period_start(period, now, tz)
    if period == Period.MONTH:
        return start.strftime("%Y-%m")
    return f"week of {start.strftime('%Y-%m-%d')}"
