from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import pytest
from dstack._internal.core.models.backends.base import BackendType
from dstack._internal.core.models.configurations import TaskConfiguration
from dstack._internal.core.models.fleets import FleetConfiguration, FleetSpec
from dstack._internal.core.models.profiles import Profile
from dstack._internal.core.models.runs import RunSpec

from ctpolicy import plugin as ctplugin
from ctpolicy.plugin import CtPolicy

UTC = ZoneInfo("UTC")
MONDAY_NOON = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)  # 2026-08-31 is a Monday

POLICY = """
version: 1
timezone: UTC
ungoverned_projects: [main]
bands:
  team-research: [70, 99]
  team-infra: [0, 39]
teams:
  team-research:
    defaults:
      windows:
        - days: [mon, tue, wed, thu, fri]
          from: "08:00"
          to: "20:00"
      max_run_duration: 12h
      cloud:
        allowed: true
        max_price: 4.0
        backends: [nebius]
    users:
      alice: {}
      bob:
        cloud:
          allowed: false
  team-infra:
    defaults:
      max_run_duration: 4h
      cloud:
        allowed: false
    users:
      erin: {}
"""


@pytest.fixture
def policy(write_policy):
    write_policy(POLICY)


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    """Pin the plugin's wall clock; window rules are otherwise time-of-day dependent."""
    holder = {"now": MONDAY_NOON}
    monkeypatch.setattr(ctplugin, "_now", lambda tz: holder["now"].astimezone(tz))
    return holder


def run_spec(
    *,
    max_duration=None,
    max_price: Optional[float] = None,
    backends=None,
    priority: Optional[int] = None,
    profile: Optional[Profile] = None,
) -> RunSpec:
    return RunSpec(
        run_name="test-run",
        configuration=TaskConfiguration(
            image="ubuntu",
            max_duration=max_duration,
            max_price=max_price,
            backends=backends,
            priority=priority,
        ),
        profile=profile or Profile(name="default"),
        ssh_key_pub="ssh-rsa AAAA",
    )


def apply(spec: RunSpec, user: str = "alice", project: str = "team-research") -> RunSpec:
    return CtPolicy().on_run_apply(user=user, project=project, spec=spec)


class TestProjectGate:
    def test_ungoverned_project_passes_through_untouched(self, policy):
        spec = run_spec(priority=50)
        result = apply(spec, user="anyone", project="main")
        assert result.configuration.priority == 50
        assert result.configuration.max_duration is None

    def test_unknown_project_is_rejected(self, policy):
        with pytest.raises(ValueError, match="has no policy"):
            apply(run_spec(), project="team-ghost")

    def test_unlisted_user_is_rejected(self, policy):
        with pytest.raises(ValueError, match="No policy entry for user 'mallory'"):
            apply(run_spec(), user="mallory")


class TestWindow:
    def test_inside_window_is_admitted(self, policy):
        assert apply(run_spec()) is not None

    def test_outside_window_is_rejected(self, policy, frozen_clock):
        frozen_clock["now"] = MONDAY_NOON.replace(hour=21)
        with pytest.raises(ValueError, match="Outside the team-research compute window"):
            apply(run_spec())

    def test_rejection_names_the_schedule_and_the_next_opening(self, policy, frozen_clock):
        frozen_clock["now"] = MONDAY_NOON.replace(hour=21)
        with pytest.raises(ValueError) as excinfo:
            apply(run_spec())
        message = str(excinfo.value)
        assert "mon/tue/wed/thu/fri 08:00-20:00" in message
        assert "Next window opens Tue 08:00" in message

    def test_weekend_is_rejected(self, policy, frozen_clock):
        frozen_clock["now"] = MONDAY_NOON + timedelta(days=5)  # Saturday
        with pytest.raises(ValueError, match="Outside the team-research compute window"):
            apply(run_spec())

    def test_team_without_windows_is_always_open(self, policy, frozen_clock):
        frozen_clock["now"] = MONDAY_NOON + timedelta(days=5)
        assert apply(run_spec(), user="erin", project="team-infra") is not None


class TestMaxDuration:
    def test_unset_duration_is_clamped_to_the_policy_ceiling(self, policy):
        # 12h ceiling, but only 8h left in the window, so the window wins.
        assert apply(run_spec()).configuration.max_duration == 8 * 3600

    def test_policy_ceiling_wins_when_it_is_tighter_than_the_window(self, policy, frozen_clock):
        frozen_clock["now"] = MONDAY_NOON.replace(hour=8)  # 12h of window left
        assert apply(run_spec()).configuration.max_duration == 12 * 3600

    def test_a_tighter_request_is_left_alone(self, policy):
        assert apply(run_spec(max_duration="1h")).configuration.max_duration == 3600

    def test_a_looser_request_is_clamped(self, policy):
        assert apply(run_spec(max_duration="24h")).configuration.max_duration == 8 * 3600

    def test_off_is_clamped(self, policy):
        """`max_duration: off` means unlimited and must not survive a ceiling."""
        assert apply(run_spec(max_duration="off")).configuration.max_duration == 8 * 3600

    def test_a_tighter_value_set_in_the_profile_is_not_widened(self, policy):
        """The run asked for 1h via `profile:`, not `configuration:`.

        Reading the request from the configuration alone would see None and
        overwrite it with the 8h ceiling, loosening a limit the user set.
        """
        spec = run_spec(profile=Profile(name="default", max_duration=3600))
        result = apply(spec)
        assert result.merged_profile.max_duration == 3600
        assert result.configuration.max_duration in (None, 3600)

    def test_infra_team_gets_its_own_shorter_ceiling(self, policy):
        result = apply(run_spec(), user="erin", project="team-infra")
        assert result.configuration.max_duration == 4 * 3600


class TestCloud:
    def test_team_without_cloud_is_pinned_to_the_on_prem_backend(self, policy):
        result = apply(run_spec(), user="erin", project="team-infra")
        assert result.configuration.backends == [BackendType.REMOTE]

    def test_explicit_cloud_request_without_permission_is_rejected(self, policy):
        with pytest.raises(ValueError, match="may not provision external cloud resources"):
            apply(run_spec(backends=[BackendType.AWS]), user="erin", project="team-infra")

    def test_explicit_on_prem_request_without_cloud_permission_is_fine(self, policy):
        result = apply(run_spec(backends=[BackendType.REMOTE]), user="erin", project="team-infra")
        assert result.configuration.backends == [BackendType.REMOTE]

    def test_user_override_can_revoke_cloud_within_a_cloud_team(self, policy):
        result = apply(run_spec(), user="bob")
        assert result.configuration.backends == [BackendType.REMOTE]

    def test_allowlist_keeps_the_on_prem_fleet_reachable(self, policy):
        """A cloud allowlist must not cut a team off from the shared SSH fleet."""
        result = apply(run_spec())
        assert BackendType.REMOTE in result.configuration.backends
        assert BackendType.NEBIUS in result.configuration.backends

    def test_requested_backend_outside_the_allowlist_is_rejected(self, policy):
        with pytest.raises(ValueError, match="may not use backends"):
            apply(run_spec(backends=[BackendType.AWS]))

    def test_allowed_backend_request_is_narrowed_to_the_intersection(self, policy):
        result = apply(run_spec(backends=[BackendType.NEBIUS, BackendType.AWS]))
        assert result.configuration.backends == [BackendType.NEBIUS]

    def test_max_price_is_clamped(self, policy):
        assert apply(run_spec(max_price=100.0)).configuration.max_price == 4.0

    def test_unset_max_price_takes_the_ceiling(self, policy):
        assert apply(run_spec()).configuration.max_price == 4.0

    def test_a_lower_max_price_is_left_alone(self, policy):
        assert apply(run_spec(max_price=1.5)).configuration.max_price == 1.5


class TestPriorityBands:
    def test_default_priority_lands_at_the_bottom_of_the_band(self, policy):
        assert apply(run_spec()).configuration.priority == 70

    def test_max_priority_lands_at_the_top_of_the_band(self, policy):
        assert apply(run_spec(priority=100)).configuration.priority == 99

    def test_priority_is_mapped_proportionally_into_the_band(self, policy):
        # Band 70-99 spans 30 values; 50 * 29 // 100 == 14.
        assert apply(run_spec(priority=50)).configuration.priority == 84

    def test_mapping_is_monotonic(self, policy):
        mapped = [apply(run_spec(priority=p)).configuration.priority for p in range(0, 101, 5)]
        assert mapped == sorted(mapped)

    def test_a_lower_band_team_never_outranks_a_higher_one(self, policy):
        top_of_infra = apply(run_spec(priority=100), user="erin", project="team-infra")
        bottom_of_research = apply(run_spec(priority=0))
        assert top_of_infra.configuration.priority < bottom_of_research.configuration.priority

    @pytest.mark.parametrize("requested", [0, 1, 37, 70, 84, 99, 100])
    def test_any_requested_priority_lands_inside_the_band(self, policy, requested):
        """The safety property: whatever a user sends, they stay in their band.

        This is what makes team priority dominate job priority, so it must hold
        for values that already look like band members, not just for 0-100 inputs
        a well-behaved client would send.
        """
        result = apply(run_spec(priority=requested)).configuration.priority
        assert 70 <= result <= 99

    def test_reapplying_a_raised_priority_stays_in_the_band(self, policy):
        """In-place updates re-run this hook, so a hand-edited priority is re-banded."""
        spec = apply(run_spec(priority=0))
        assert spec.configuration.priority == 70
        spec.configuration.priority = 100  # what a user could send on re-apply
        assert apply(spec).configuration.priority == 99


class TestRepeatedApplication:
    def test_the_plan_and_apply_calls_agree(self, policy):
        """dstack calls the hook once for the plan and again for the apply.

        Both calls receive the original spec — `ApplyPolicy.on_apply` documents
        it, and `ApplyRunPlanInput` carries only `run_spec`, so the client cannot
        send our mutated spec back. The property to hold is therefore that two
        calls on equal inputs agree, which is what makes the plan table an honest
        preview of what will be submitted.
        """
        planned = apply(run_spec(max_duration="24h", max_price=100.0, priority=50))
        applied = apply(run_spec(max_duration="24h", max_price=100.0, priority=50))
        assert planned.configuration.dict() == applied.configuration.dict()

    def test_clamps_are_stable_under_re_application(self, policy):
        """Everything except the band mapping is a min/intersect, so it settles."""
        once = apply(run_spec(max_duration="24h", max_price=100.0))
        twice = apply(once)
        assert twice.configuration.max_duration == once.configuration.max_duration
        assert twice.configuration.max_price == once.configuration.max_price
        assert twice.configuration.backends == once.configuration.backends


class TestFleets:
    def ssh_fleet(self) -> FleetSpec:
        return FleetSpec(
            configuration=FleetConfiguration.parse_obj(
                {"name": "onprem", "ssh_config": {"hosts": ["10.0.0.2"]}}
            ),
            profile=Profile(name="default"),
        )

    def cloud_fleet(self) -> FleetSpec:
        return FleetSpec(
            configuration=FleetConfiguration(name="cloudy", nodes=1),
            profile=Profile(name="default"),
        )

    def test_cloud_fleet_rejected_for_a_team_without_cloud(self, policy):
        with pytest.raises(ValueError, match="a fleet without `ssh_config` is a cloud fleet"):
            CtPolicy().on_fleet_apply(user="erin", project="team-infra", spec=self.cloud_fleet())

    def test_ssh_fleet_allowed_for_a_team_without_cloud(self, policy):
        spec = self.ssh_fleet()
        assert CtPolicy().on_fleet_apply(user="erin", project="team-infra", spec=spec) is spec

    def test_cloud_fleet_allowed_for_a_cloud_team(self, policy):
        spec = self.cloud_fleet()
        assert CtPolicy().on_fleet_apply(user="alice", project="team-research", spec=spec) is spec

    def test_ungoverned_project_is_exempt(self, policy):
        spec = self.cloud_fleet()
        assert CtPolicy().on_fleet_apply(user="admin", project="main", spec=spec) is spec
