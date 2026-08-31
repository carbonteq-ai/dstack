"""Budget enforcement at admission (phase 2)."""

from datetime import datetime, timedelta, timezone

import pytest
from dstack._internal.core.models.backends.base import BackendType
from dstack._internal.core.models.configurations import TaskConfiguration
from dstack._internal.core.models.profiles import Profile
from dstack._internal.core.models.runs import RunSpec

from ctpolicy import plugin as ctplugin
from ctpolicy import usage as usage_module
from ctpolicy.config import Period
from ctpolicy.plugin import CtPolicy
from ctpolicy.usage import Amounts, ScopeUsage, Snapshot, TeamUsage

UTC = timezone.utc
NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)  # a Wednesday, inside the window

POLICY = """
version: 1
timezone: UTC
usage_snapshot_max_age: 180s
ungoverned_projects: [main]
bands:
  team-research: [70, 99]
  team-free: [0, 39]
teams:
  team-research:
    defaults:
      max_run_duration: 12h
      time_budget: {period: month, limit: 100h}
      cloud:
        allowed: true
        max_price: 10.0
        dollar_budget: {period: month, limit: 1000}
    users:
      alice: {}
      bob:
        time_budget: {period: month, limit: 10h}
  team-free:
    defaults:
      max_run_duration: 4h
    users:
      erin: {}
"""


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    monkeypatch.setattr(ctplugin, "_now", lambda tz: NOW.astimezone(tz))


@pytest.fixture
def policy(write_policy):
    write_policy(POLICY)


def write_snapshot(
    path,
    *,
    team_seconds=0.0,
    team_dollars=0.0,
    team_committed=Amounts(),
    user="alice",
    user_seconds=0.0,
    user_dollars=0.0,
    user_committed=Amounts(),
    generated_at=None,
):
    snapshot = Snapshot(
        generated_at=generated_at or NOW,
        teams={
            "team-research": TeamUsage(
                team=ScopeUsage(
                    spent={Period.MONTH: Amounts(seconds=team_seconds, dollars=team_dollars)},
                    committed=team_committed,
                ),
                users={
                    user: ScopeUsage(
                        spent={Period.MONTH: Amounts(seconds=user_seconds, dollars=user_dollars)},
                        committed=user_committed,
                    )
                },
            )
        },
    )
    usage_module.write(snapshot, path)
    return snapshot


def spec(max_duration=None, max_price=None, backends=None, priority=None) -> RunSpec:
    return RunSpec(
        run_name="test-run",
        configuration=TaskConfiguration(
            image="ubuntu",
            max_duration=max_duration,
            max_price=max_price,
            backends=backends,
            priority=priority,
        ),
        profile=Profile(name="default"),
        ssh_key_pub="ssh-rsa AAAA",
    )


def apply(s, user="alice", project="team-research"):
    return CtPolicy().on_run_apply(user=user, project=project, spec=s)


class TestSnapshotAvailability:
    def test_team_without_budgets_is_unaffected_by_a_missing_snapshot(self, policy, snapshot_path):
        """A dead enforcer must not take down teams that configure no budgets."""
        assert not snapshot_path.exists()
        result = apply(spec(), user="erin", project="team-free")
        assert result.configuration.max_duration == 4 * 3600

    def test_budgeted_team_is_denied_when_the_snapshot_is_missing(self, policy, snapshot_path):
        with pytest.raises(ValueError, match="Budget usage is currently unknown"):
            apply(spec())

    def test_budgeted_team_is_denied_when_the_snapshot_is_stale(self, policy, snapshot_path):
        write_snapshot(snapshot_path, generated_at=NOW - timedelta(hours=1))
        with pytest.raises(ValueError, match="past the"):
            apply(spec())

    def test_allow_mode_admits_when_usage_is_unknown(self, write_policy, snapshot_path):
        write_policy(POLICY.replace("version: 1", "version: 1\non_usage_unavailable: allow"))
        result = apply(spec())
        assert result.configuration.max_duration == 12 * 3600  # only the team ceiling applies


class TestTimeBudget:
    def test_admits_and_clamps_to_what_is_left(self, policy, snapshot_path):
        # 100h limit, 95h used -> 5h left, tighter than the 12h ceiling.
        write_snapshot(snapshot_path, team_seconds=95 * 3600)
        assert apply(spec()).configuration.max_duration == 5 * 3600

    def test_team_ceiling_still_wins_when_it_is_tighter(self, policy, snapshot_path):
        write_snapshot(snapshot_path, team_seconds=10 * 3600)  # 90h left
        assert apply(spec()).configuration.max_duration == 12 * 3600

    def test_committed_time_is_subtracted(self, policy, snapshot_path):
        """Two runs each under budget must not be able to exceed it together."""
        write_snapshot(
            snapshot_path,
            team_seconds=90 * 3600,
            team_committed=Amounts(seconds=8 * 3600),
        )
        assert apply(spec()).configuration.max_duration == 2 * 3600

    def test_exhausted_budget_is_rejected(self, policy, snapshot_path):
        write_snapshot(snapshot_path, team_seconds=100 * 3600)
        with pytest.raises(ValueError, match="has no time budget left"):
            apply(spec())

    def test_rejection_states_the_numbers(self, policy, snapshot_path):
        write_snapshot(
            snapshot_path, team_seconds=100 * 3600, team_committed=Amounts(seconds=3600)
        )
        with pytest.raises(ValueError) as excinfo:
            apply(spec())
        message = str(excinfo.value)
        assert "100h allowed" in message
        assert "100h already used" in message
        assert "1h committed" in message

    def test_exhausted_by_committed_alone_is_rejected(self, policy, snapshot_path):
        write_snapshot(snapshot_path, team_committed=Amounts(seconds=100 * 3600))
        with pytest.raises(ValueError, match="has no time budget left"):
            apply(spec())


class TestUserVersusTeamBudget:
    def test_user_budget_binds_when_tighter(self, policy, snapshot_path):
        # bob has 10h; the team has 100h with 0 used.
        write_snapshot(snapshot_path, user="bob", user_seconds=2 * 3600)
        assert apply(spec(), user="bob").configuration.max_duration == 8 * 3600

    def test_team_budget_binds_even_when_the_user_has_room(self, policy, snapshot_path):
        """A user slice cannot exceed the pool it is carved from."""
        write_snapshot(snapshot_path, team_seconds=97 * 3600, user="bob", user_seconds=0)
        assert apply(spec(), user="bob").configuration.max_duration == 3 * 3600

    def test_exhausted_user_budget_names_the_user(self, policy, snapshot_path):
        write_snapshot(snapshot_path, user="bob", user_seconds=10 * 3600)
        with pytest.raises(ValueError, match="User 'bob' has no time budget left"):
            apply(spec(), user="bob")

    def test_a_user_without_their_own_budget_only_faces_the_team_pool(self, policy, snapshot_path):
        write_snapshot(snapshot_path, user="alice", user_seconds=99 * 3600, team_seconds=0)
        # alice has no user budget, so her own 99h does not bind; the team's does.
        assert apply(spec()).configuration.max_duration == 12 * 3600


class TestDollarBudget:
    def test_duration_is_clamped_so_worst_case_cost_fits(self, policy, snapshot_path):
        # $1000 limit, $980 used -> $20 left at $10/hr -> 2h.
        write_snapshot(snapshot_path, team_dollars=980.0)
        assert apply(spec()).configuration.max_duration == 2 * 3600

    def test_committed_dollars_are_subtracted(self, policy, snapshot_path):
        write_snapshot(snapshot_path, team_dollars=900.0, team_committed=Amounts(dollars=90.0))
        assert apply(spec()).configuration.max_duration == 1 * 3600

    def test_exhausted_cloud_budget_falls_back_to_on_prem(self, policy, snapshot_path):
        """Running out of money stops spending, not working."""
        write_snapshot(snapshot_path, team_dollars=1000.0)
        result = apply(spec())
        assert result.configuration.backends == [BackendType.REMOTE]

    def test_on_prem_fallback_is_not_shortened_by_the_dollar_budget(self, policy, snapshot_path):
        """A run pinned to the free fleet must keep the full time ceiling."""
        write_snapshot(snapshot_path, team_dollars=1000.0)
        assert apply(spec()).configuration.max_duration == 12 * 3600

    def test_explicit_cloud_request_is_rejected_when_the_budget_is_gone(
        self, policy, snapshot_path
    ):
        write_snapshot(snapshot_path, team_dollars=1000.0)
        with pytest.raises(ValueError, match="has no cloud budget left"):
            apply(spec(backends=[BackendType.NEBIUS]))

    def test_explicitly_on_prem_run_is_unaffected_by_an_exhausted_budget(
        self, policy, snapshot_path
    ):
        write_snapshot(snapshot_path, team_dollars=1000.0)
        result = apply(spec(backends=[BackendType.REMOTE]))
        assert result.configuration.backends == [BackendType.REMOTE]
        assert result.configuration.max_duration == 12 * 3600

    def test_a_run_pinned_on_prem_ignores_a_low_dollar_budget(self, policy, snapshot_path):
        """$1 left would otherwise clamp the run to six minutes for nothing."""
        write_snapshot(snapshot_path, team_dollars=999.0)
        result = apply(spec(backends=[BackendType.REMOTE]))
        assert result.configuration.max_duration == 12 * 3600

    def test_a_cloud_capable_run_is_clamped_by_a_low_dollar_budget(self, policy, snapshot_path):
        write_snapshot(snapshot_path, team_dollars=999.0)
        result = apply(spec(backends=[BackendType.NEBIUS]))
        assert result.configuration.max_duration == 360  # $1 at $10/hr


class TestInteractionWithPhaseOne:
    def test_priority_band_still_applies(self, policy, snapshot_path):
        write_snapshot(snapshot_path)
        assert apply(spec(priority=100)).configuration.priority == 99

    def test_max_price_is_still_clamped(self, policy, snapshot_path):
        write_snapshot(snapshot_path)
        assert apply(spec(max_price=500.0)).configuration.max_price == 10.0

    def test_ungoverned_project_never_reads_the_snapshot(self, policy, snapshot_path):
        assert not snapshot_path.exists()
        result = CtPolicy().on_run_apply(user="admin", project="main", spec=spec(priority=50))
        assert result.configuration.priority == 50
