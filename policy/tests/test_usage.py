from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import factories
import pytest
from dstack._internal.core.models.backends.base import BackendType
from dstack._internal.core.models.runs import JobStatus, RunStatus

from ctpolicy import usage
from ctpolicy.config import Period
from ctpolicy.usage import Amounts, ScopeUsage, Snapshot, SnapshotUnavailable, TeamUsage

UTC = timezone.utc
KARACHI = ZoneInfo("Asia/Karachi")

# 2026-08-12 is a Wednesday.
NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)


class TestPeriodStart:
    def test_month_starts_on_the_first(self):
        start = usage.period_start(Period.MONTH, NOW, UTC)
        assert (start.year, start.month, start.day) == (2026, 8, 1)
        assert (start.hour, start.minute) == (0, 0)

    def test_week_starts_on_monday(self):
        start = usage.period_start(Period.WEEK, NOW, UTC)
        assert start.weekday() == 0
        assert start.day == 10  # the Monday before Wed the 12th

    def test_period_is_evaluated_in_the_config_timezone(self):
        """Just after UTC midnight on the 1st is still the previous month in Karachi."""
        just_past_utc_midnight = datetime(2026, 8, 1, 2, 0, tzinfo=UTC)
        start = usage.period_start(Period.MONTH, just_past_utc_midnight, KARACHI)
        assert (start.year, start.month) == (2026, 8)
        # 2026-07-31 21:00 UTC is 02:00 on the 1st in Karachi, so still August.
        earlier = datetime(2026, 7, 31, 21, 0, tzinfo=UTC)
        assert usage.period_start(Period.MONTH, earlier, KARACHI).month == 8


class TestAccrue:
    def test_finished_run_counts_its_whole_duration(self):
        start = NOW - timedelta(hours=3)
        run = factories.run(
            submitted_at=start,
            status=RunStatus.DONE,
            submissions=[
                factories.submission(submitted_at=start, finished_at=NOW, status=JobStatus.DONE)
            ],
        )
        spent, committed, unbounded = usage.accrue(
            run, NOW, usage.period_start(Period.MONTH, NOW, UTC)
        )
        assert spent.seconds == 3 * 3600
        assert committed.seconds == 0
        assert unbounded == 0

    def test_active_run_counts_elapsed_and_commits_the_remainder(self):
        start = NOW - timedelta(hours=2)
        run = factories.run(
            submitted_at=start,
            max_duration=6 * 3600,
            submissions=[factories.submission(submitted_at=start)],
        )
        spent, committed, _ = usage.accrue(run, NOW, usage.period_start(Period.MONTH, NOW, UTC))
        assert spent.seconds == 2 * 3600
        assert committed.seconds == 4 * 3600
        # Spent plus committed is exactly the ceiling the run was admitted with.
        assert spent.seconds + committed.seconds == 6 * 3600

    def test_a_run_past_its_ceiling_commits_nothing_further(self):
        start = NOW - timedelta(hours=9)
        run = factories.run(
            submitted_at=start,
            max_duration=6 * 3600,
            submissions=[factories.submission(submitted_at=start)],
        )
        _, committed, _ = usage.accrue(run, NOW, usage.period_start(Period.MONTH, NOW, UTC))
        assert committed.seconds == 0

    def test_only_the_part_inside_the_period_is_charged(self):
        """A run spanning the period boundary must not charge last month twice."""
        start = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
        run = factories.run(
            submitted_at=start,
            status=RunStatus.DONE,
            submissions=[
                factories.submission(submitted_at=start, finished_at=NOW, status=JobStatus.DONE)
            ],
        )
        month_start = usage.period_start(Period.MONTH, NOW, UTC)
        spent, _, _ = usage.accrue(run, NOW, month_start)
        assert spent.seconds == (NOW - month_start).total_seconds()

    def test_multi_node_run_accrues_once_per_submission(self):
        start = NOW - timedelta(hours=1)
        run = factories.run(
            submitted_at=start,
            max_duration=4 * 3600,
            submissions=[
                factories.submission(submitted_at=start),
                factories.submission(submitted_at=start),
            ],
        )
        spent, committed, _ = usage.accrue(run, NOW, usage.period_start(Period.MONTH, NOW, UTC))
        assert spent.seconds == 2 * 3600
        assert committed.seconds == 2 * 3 * 3600

    def test_missing_max_duration_is_reported_not_guessed(self):
        start = NOW - timedelta(hours=1)
        run = factories.run(
            submitted_at=start,
            max_duration=None,
            submissions=[factories.submission(submitted_at=start)],
        )
        _, committed, unbounded = usage.accrue(
            run, NOW, usage.period_start(Period.MONTH, NOW, UTC)
        )
        assert committed.seconds == 0
        assert unbounded == 1


class TestDollars:
    def test_on_prem_run_commits_no_dollars(self):
        """dstack prices SSH instances at zero, so on-prem cannot spend budget."""
        start = NOW - timedelta(hours=1)
        run = factories.run(
            submitted_at=start,
            max_duration=4 * 3600,
            max_price=10.0,
            backends=[BackendType.REMOTE],
            submissions=[factories.submission(submitted_at=start, backend=BackendType.REMOTE)],
        )
        _, committed, _ = usage.accrue(run, NOW, usage.period_start(Period.MONTH, NOW, UTC))
        assert committed.dollars == 0

    def test_cloud_run_commits_its_worst_case_spend(self):
        start = NOW - timedelta(hours=1)
        run = factories.run(
            submitted_at=start,
            max_duration=4 * 3600,
            max_price=10.0,
            backends=[BackendType.AWS],
            submissions=[factories.submission(submitted_at=start, backend=BackendType.AWS)],
        )
        _, committed, _ = usage.accrue(run, NOW, usage.period_start(Period.MONTH, NOW, UTC))
        assert committed.dollars == pytest.approx(30.0)  # 3h left x $10/hr

    def test_unplaced_run_is_assumed_to_cost_money(self):
        """Before provisioning there is no backend to check, so assume the worst."""
        start = NOW - timedelta(minutes=1)
        run = factories.run(
            submitted_at=start,
            max_duration=3600,
            max_price=4.0,
            submissions=[factories.submission(submitted_at=start, backend=None)],
        )
        _, committed, _ = usage.accrue(run, NOW, usage.period_start(Period.MONTH, NOW, UTC))
        assert committed.dollars > 0

    def test_spent_dollars_come_from_the_servers_own_cost(self):
        start = NOW - timedelta(hours=2)
        run = factories.run(
            submitted_at=start,
            status=RunStatus.DONE,
            cost=7.5,
            submissions=[
                factories.submission(submitted_at=start, finished_at=NOW, status=JobStatus.DONE)
            ],
        )
        spent, _, _ = usage.accrue(run, NOW, usage.period_start(Period.MONTH, NOW, UTC))
        assert spent.dollars == pytest.approx(7.5)

    def test_cost_is_split_across_a_period_boundary(self):
        start = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)  # 12h before August
        end = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)  # 12h into August
        run = factories.run(
            submitted_at=start,
            status=RunStatus.DONE,
            cost=100.0,
            submissions=[
                factories.submission(submitted_at=start, finished_at=end, status=JobStatus.DONE)
            ],
        )
        month_start = usage.period_start(Period.MONTH, end, UTC)
        spent, _, _ = usage.accrue(run, end, month_start)
        assert spent.dollars == pytest.approx(50.0)


class TestBuild:
    def test_aggregates_by_team_and_user(self):
        start = NOW - timedelta(hours=1)
        runs = [
            factories.run(
                project="team-research",
                user="alice",
                submitted_at=start,
                max_duration=3600,
                submissions=[factories.submission(submitted_at=start)],
            ),
            factories.run(
                project="team-research",
                user="bob",
                submitted_at=start,
                max_duration=3600,
                submissions=[factories.submission(submitted_at=start)],
            ),
        ]
        snapshot = usage.build(runs, ["team-research"], UTC, NOW)
        team = snapshot.scope("team-research")
        assert team.usage_for(Period.MONTH).seconds == 2 * 3600
        assert snapshot.scope("team-research", "alice").usage_for(Period.MONTH).seconds == 3600

    def test_runs_from_ungoverned_projects_are_ignored(self):
        start = NOW - timedelta(hours=1)
        runs = [
            factories.run(
                project="main",
                user="admin",
                submitted_at=start,
                submissions=[factories.submission(submitted_at=start)],
            )
        ]
        snapshot = usage.build(runs, ["team-research"], UTC, NOW)
        assert snapshot.scope("team-research").usage_for(Period.MONTH).seconds == 0

    def test_committed_is_counted_once_not_once_per_period(self):
        """It is a forward-looking figure, so it must not double when both
        periods are computed."""
        start = NOW - timedelta(hours=1)
        runs = [
            factories.run(
                submitted_at=start,
                max_duration=5 * 3600,
                submissions=[factories.submission(submitted_at=start)],
            )
        ]
        snapshot = usage.build(runs, ["team-research"], UTC, NOW)
        assert snapshot.scope("team-research").committed.seconds == 4 * 3600

    def test_both_periods_are_always_populated(self):
        start = NOW - timedelta(hours=1)
        runs = [
            factories.run(
                submitted_at=start,
                max_duration=3600,
                submissions=[factories.submission(submitted_at=start)],
            )
        ]
        snapshot = usage.build(runs, ["team-research"], UTC, NOW)
        scope = snapshot.scope("team-research")
        assert Period.MONTH in scope.spent
        assert Period.WEEK in scope.spent


class TestSnapshotIO:
    def snapshot(self, generated_at=NOW) -> Snapshot:
        return Snapshot(
            generated_at=generated_at,
            teams={
                "team-research": TeamUsage(
                    team=ScopeUsage(spent={Period.MONTH: Amounts(seconds=3600, dollars=5.0)}),
                    users={"alice": ScopeUsage(committed=Amounts(seconds=60))},
                )
            },
        )

    def test_round_trip(self, tmp_path):
        path = tmp_path / "usage.json"
        usage.write(self.snapshot(), path)
        loaded = usage.load(path)
        assert loaded.scope("team-research").usage_for(Period.MONTH).seconds == 3600
        assert loaded.scope("team-research", "alice").committed.seconds == 60

    def test_write_creates_the_directory(self, tmp_path):
        path = tmp_path / "nested" / "usage.json"
        usage.write(self.snapshot(), path)
        assert path.exists()

    def test_write_leaves_no_temporary_files(self, tmp_path):
        path = tmp_path / "usage.json"
        usage.write(self.snapshot(), path)
        assert [p.name for p in tmp_path.iterdir()] == ["usage.json"]

    def test_missing_file_is_unavailable_not_empty(self, tmp_path):
        with pytest.raises(SnapshotUnavailable, match="cannot read"):
            usage.load(tmp_path / "absent.json")

    def test_garbage_is_unavailable(self, tmp_path):
        path = tmp_path / "usage.json"
        path.write_text("{not json")
        with pytest.raises(SnapshotUnavailable, match="not a usable snapshot"):
            usage.load(path)

    def test_a_future_schema_version_is_refused(self, tmp_path):
        path = tmp_path / "usage.json"
        path.write_text(Snapshot(version=99, generated_at=NOW).json())
        with pytest.raises(SnapshotUnavailable, match="version 99"):
            usage.load(path)

    def test_stale_snapshot_is_unavailable(self, tmp_path):
        path = tmp_path / "usage.json"
        usage.write(self.snapshot(generated_at=NOW - timedelta(hours=1)), path)
        with pytest.raises(SnapshotUnavailable, match="past the"):
            usage.load(path, max_age=180, now=NOW)

    def test_fresh_snapshot_is_accepted(self, tmp_path):
        path = tmp_path / "usage.json"
        usage.write(self.snapshot(generated_at=NOW - timedelta(seconds=30)), path)
        assert usage.load(path, max_age=180, now=NOW) is not None


class TestFormatting:
    @pytest.mark.parametrize(
        "seconds,expected",
        [(0, "0m"), (60, "1m"), (3600, "1h"), (5400, "1h30m"), (400 * 3600, "400h")],
    )
    def test_format_duration(self, seconds, expected):
        assert usage.format_duration(seconds) == expected

    def test_period_label(self):
        assert usage.period_label(Period.MONTH, NOW, UTC) == "2026-08"
        assert usage.period_label(Period.WEEK, NOW, UTC) == "week of 2026-08-10"
