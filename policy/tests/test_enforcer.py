"""The enforcer backstop (phase 4).

Covers what it stops and — just as important — what it leaves alone. Requirement
9 allows termination only for a time window, a duration ceiling or a budget, so
the negative cases here are the specification, not filler.
"""

from datetime import datetime, timedelta, timezone

import factories
import pytest
import yaml
from dstack._internal.core.models.runs import JobStatus, RunStatus

from ctpolicy import enforcer
from ctpolicy import usage as usage_module
from ctpolicy.config import Period, PolicyConfig
from ctpolicy.usage import Amounts, ScopeUsage, Snapshot, TeamUsage

UTC = timezone.utc
NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)  # Wednesday noon
WEEKEND = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)  # Saturday noon

POLICY_YAML = """
version: 1
timezone: UTC
ungoverned_projects: [main]
bands:
  team-research: [70, 99]
teams:
  team-research:
    defaults:
      windows:
        - days: [mon, tue, wed, thu, fri]
          from: "08:00"
          to: "20:00"
      max_run_duration: 4h
      time_budget: {period: month, limit: 100h}
      cloud:
        allowed: true
        dollar_budget: {period: month, limit: 1000}
    users:
      alice: {}
      bob:
        time_budget: {period: month, limit: 10h}
"""


@pytest.fixture
def config() -> PolicyConfig:
    return PolicyConfig.parse_obj(yaml.safe_load(POLICY_YAML))


def snapshot(*, team_seconds=0.0, team_dollars=0.0, user="alice", user_seconds=0.0) -> Snapshot:
    return Snapshot(
        generated_at=NOW,
        teams={
            "team-research": TeamUsage(
                team=ScopeUsage(
                    spent={Period.MONTH: Amounts(seconds=team_seconds, dollars=team_dollars)}
                ),
                users={user: ScopeUsage(spent={Period.MONTH: Amounts(seconds=user_seconds)})},
            )
        },
    )


def active_run(*, submitted_at=None, user="alice", project="team-research", name="r1"):
    submitted_at = submitted_at or NOW - timedelta(minutes=10)
    return factories.run(
        project=project,
        user=user,
        run_name=name,
        submitted_at=submitted_at,
        status=RunStatus.RUNNING,
        submissions=[factories.submission(submitted_at=submitted_at)],
    )


class TestLeavesRunsAlone:
    def test_a_healthy_run_inside_its_window_is_untouched(self, config):
        breaches = enforcer.find_breaches([active_run()], config, snapshot(), NOW)
        assert breaches == []

    def test_finished_runs_are_ignored(self, config):
        run = factories.run(
            status=RunStatus.DONE,
            submitted_at=NOW - timedelta(days=3),
            submissions=[
                factories.submission(
                    submitted_at=NOW - timedelta(days=3),
                    finished_at=NOW - timedelta(days=3),
                    status=JobStatus.DONE,
                )
            ],
        )
        assert enforcer.find_breaches([run], config, snapshot(), WEEKEND) == []

    def test_ungoverned_projects_are_never_touched(self, config):
        run = active_run(project="main", user="admin")
        assert enforcer.find_breaches([run], config, snapshot(), WEEKEND) == []

    def test_unknown_projects_are_never_touched(self, config):
        run = active_run(project="some-other-project")
        assert enforcer.find_breaches([run], config, snapshot(), WEEKEND) == []

    def test_a_user_removed_from_policy_keeps_running(self, config):
        """Removing someone is not one of the three reasons a run may be stopped."""
        run = active_run(user="mallory")
        assert enforcer.find_breaches([run], config, snapshot(), NOW) == []

    def test_priority_is_never_a_reason(self, config):
        """Nothing may be preempted to free capacity for a higher-priority team."""
        low = active_run(name="low")
        high = active_run(name="high")
        assert enforcer.find_breaches([low, high], config, snapshot(), NOW) == []


class TestWindowBreach:
    def test_a_run_outside_the_window_is_stopped(self, config):
        breaches = enforcer.find_breaches([active_run()], config, snapshot(), WEEKEND)
        assert len(breaches) == 1
        assert "outside the team-research compute window" in breaches[0].reason

    def test_the_breach_names_the_run_and_its_owner(self, config):
        breaches = enforcer.find_breaches(
            [active_run(name="train-42", user="bob")], config, snapshot(), WEEKEND
        )
        assert (breaches[0].run_name, breaches[0].user) == ("train-42", "bob")


class TestDurationBreach:
    def test_a_run_past_the_ceiling_plus_grace_is_stopped(self, config):
        old = (
            NOW - timedelta(hours=4) - timedelta(seconds=enforcer.PROVISIONING_GRACE_SECONDS + 60)
        )
        breaches = enforcer.find_breaches([active_run(submitted_at=old)], config, snapshot(), NOW)
        assert len(breaches) == 1
        assert "past the 4h ceiling" in breaches[0].reason

    def test_a_run_inside_the_grace_is_left_alone(self, config):
        """The runner's clock starts at run time; ours can only see submission,
        so a queued run must not be killed for time it never spent computing."""
        recent = NOW - timedelta(hours=4) - timedelta(seconds=60)
        assert (
            enforcer.find_breaches([active_run(submitted_at=recent)], config, snapshot(), NOW)
            == []
        )


class TestBudgetBreach:
    def test_over_team_time_budget_stops_the_run(self, config):
        breaches = enforcer.find_breaches(
            [active_run()], config, snapshot(team_seconds=101 * 3600), NOW
        )
        assert "over its time budget" in breaches[0].reason

    def test_over_user_time_budget_stops_only_that_user(self, config):
        snap = snapshot(user="bob", user_seconds=11 * 3600)
        runs = [active_run(user="alice", name="a"), active_run(user="bob", name="b")]
        breaches = enforcer.find_breaches(runs, config, snap, NOW)
        assert [b.run_name for b in breaches] == ["b"]
        assert "user 'bob'" in breaches[0].reason

    def test_over_dollar_budget_stops_the_run(self, config):
        breaches = enforcer.find_breaches(
            [active_run()], config, snapshot(team_dollars=1000.0), NOW
        )
        assert "over its cloud budget" in breaches[0].reason

    def test_committed_alone_does_not_stop_a_run(self, config):
        """Committed is the run's own remaining entitlement; counting it here
        would stop every run the moment it started."""
        snap = snapshot()
        snap.teams["team-research"].team.committed = Amounts(seconds=200 * 3600)
        assert enforcer.find_breaches([active_run()], config, snap, NOW) == []


class _FakeRuns:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []
        self.stopped = []

    def list(self, **kwargs):
        self.calls.append(kwargs)
        key = "active" if kwargs["only_active"] else "all"
        queue = self.pages.get(key, [])
        return queue.pop(0) if queue else []

    def stop(self, project_name, runs_names, abort):
        self.stopped.append((project_name, runs_names, abort))


class _FakeClient:
    def __init__(self, pages):
        self.runs = _FakeRuns(pages)


class TestFetching:
    def test_asks_for_every_project(self, config):
        client = _FakeClient({"active": [[]], "all": [[]]})
        enforcer.fetch_runs(client, config, NOW)
        assert all(call["project_name"] is None for call in client.runs.calls)

    def test_never_limits_job_submissions(self, config):
        """Cost is summed over submissions, so a limit would under-count spend."""
        client = _FakeClient({"active": [[]], "all": [[]]})
        enforcer.fetch_runs(client, config, NOW)
        assert all(call["job_submissions_limit"] is None for call in client.runs.calls)
        assert all(call["include_jobs"] for call in client.runs.calls)

    def test_active_and_historical_runs_are_both_fetched(self, config):
        active = active_run(name="live")
        finished = factories.run(
            run_name="old",
            submitted_at=NOW - timedelta(hours=2),
            status=RunStatus.DONE,
            submissions=[
                factories.submission(
                    submitted_at=NOW - timedelta(hours=2),
                    finished_at=NOW - timedelta(hours=1),
                    status=JobStatus.DONE,
                )
            ],
        )
        client = _FakeClient({"active": [[active]], "all": [[finished]]})
        runs = enforcer.fetch_runs(client, config, NOW)
        assert {r.run_spec.run_name for r in runs} == {"live", "old"}

    def test_a_run_in_both_queries_is_counted_once(self, config):
        run = active_run(name="live")
        client = _FakeClient({"active": [[run]], "all": [[run]]})
        assert len(enforcer.fetch_runs(client, config, NOW)) == 1

    def test_history_stops_once_it_predates_the_period(self, config):
        old = factories.run(
            run_name="ancient",
            submitted_at=NOW - timedelta(days=90),
            status=RunStatus.DONE,
            submissions=[
                factories.submission(
                    submitted_at=NOW - timedelta(days=90),
                    finished_at=NOW - timedelta(days=90),
                    status=JobStatus.DONE,
                )
            ],
        )
        # A second page would be requested only if the first did not predate the
        # earliest period start.
        client = _FakeClient({"active": [[]], "all": [[old], [old]]})
        enforcer.fetch_runs(client, config, NOW)
        history_calls = [c for c in client.runs.calls if not c["only_active"]]
        assert len(history_calls) == 1


class TestCycle:
    def test_writes_a_snapshot_and_stops_breaches(self, monkeypatch, tmp_path, write_policy):
        write_policy(POLICY_YAML)
        path = tmp_path / "usage.json"
        monkeypatch.setenv(usage_module.SNAPSHOT_FILE_ENV_VAR, str(path))
        monkeypatch.setattr(enforcer, "datetime", _FrozenDatetime(WEEKEND))

        client = _FakeClient({"active": [[active_run(name="weekend-run")]], "all": [[]]})
        enforcer.run_cycle(client)

        assert path.exists()
        assert client.runs.stopped == [("team-research", ["weekend-run"], False)]

    def test_dry_run_writes_the_snapshot_but_stops_nothing(
        self, monkeypatch, tmp_path, write_policy
    ):
        write_policy(POLICY_YAML)
        path = tmp_path / "usage.json"
        monkeypatch.setenv(usage_module.SNAPSHOT_FILE_ENV_VAR, str(path))
        monkeypatch.setattr(enforcer, "datetime", _FrozenDatetime(WEEKEND))

        client = _FakeClient({"active": [[active_run(name="weekend-run")]], "all": [[]]})
        enforcer.run_cycle(client, dry_run=True)

        assert path.exists()
        assert client.runs.stopped == []

    def test_stops_gracefully_so_stop_duration_is_honoured(
        self, monkeypatch, tmp_path, write_policy
    ):
        write_policy(POLICY_YAML)
        monkeypatch.setenv(usage_module.SNAPSHOT_FILE_ENV_VAR, str(tmp_path / "usage.json"))
        monkeypatch.setattr(enforcer, "datetime", _FrozenDatetime(WEEKEND))
        client = _FakeClient({"active": [[active_run(name="r")]], "all": [[]]})
        enforcer.run_cycle(client)
        _, _, abort = client.runs.stopped[0]
        assert abort is False


class _FrozenDatetime:
    """Stand-in for the `datetime` module with `now()` pinned."""

    def __init__(self, instant: datetime):
        self._instant = instant

    def now(self, tz=None):
        return self._instant.astimezone(tz) if tz else self._instant
