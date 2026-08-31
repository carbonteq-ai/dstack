import pytest

from ctpolicy import config as policy_config
from ctpolicy.config import PolicyConfig

MINIMAL = """
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
      alice:
        max_run_duration: 24h
      bob:
        cloud:
          allowed: false
      carol: {}
      dan:
  team-infra:
    defaults:
      max_run_duration: 4h
      cloud:
        allowed: false
    users:
      erin: {}
"""


def parse(text: str) -> PolicyConfig:
    import yaml

    return PolicyConfig.parse_obj(yaml.safe_load(text))


class TestParsing:
    def test_minimal_config(self):
        config = parse(MINIMAL)
        assert set(config.teams) == {"team-research", "team-infra"}
        assert config.band("team-research") == (70, 99)
        assert config.ungoverned_projects == ["main"]

    def test_duration_uses_dstack_syntax(self):
        config = parse(MINIMAL)
        assert config.teams["team-research"].defaults.max_run_duration == 12 * 3600
        assert config.teams["team-research"].users["alice"].max_run_duration == 24 * 3600

    def test_window_parsed_into_weekdays_and_seconds(self):
        w = parse(MINIMAL).teams["team-research"].defaults.windows[0]
        assert w.weekdays == frozenset({0, 1, 2, 3, 4})
        assert (w.start_seconds, w.end_seconds) == (8 * 3600, 20 * 3600)
        assert w.pretty() == "mon/tue/wed/thu/fri 08:00-20:00"

    def test_bare_username_means_inherit(self):
        """`dan:` with nothing after it is YAML null, not a missing entry."""
        users = parse(MINIMAL).teams["team-research"].users
        assert "dan" in users
        assert users["dan"].max_run_duration is None

    def test_unknown_key_is_rejected(self):
        with pytest.raises(Exception, match="max_runtime"):
            parse(MINIMAL.replace("max_run_duration: 4h", "max_runtime: 4h"))


class TestValidation:
    def test_overlapping_bands_rejected(self):
        with pytest.raises(Exception, match="overlap"):
            parse(MINIMAL.replace("team-infra: [0, 39]", "team-infra: [0, 75]"))

    def test_band_outside_dstack_range_rejected(self):
        with pytest.raises(Exception, match="priority range"):
            parse(MINIMAL.replace("team-research: [70, 99]", "team-research: [70, 120]"))

    def test_inverted_band_rejected(self):
        with pytest.raises(Exception, match="inverted"):
            parse(MINIMAL.replace("team-research: [70, 99]", "team-research: [99, 70]"))

    def test_team_without_band_rejected(self):
        with pytest.raises(Exception, match="no priority band"):
            parse(MINIMAL.replace("  team-infra: [0, 39]\n", ""))

    def test_band_without_team_rejected(self):
        with pytest.raises(Exception, match="do not exist"):
            parse(MINIMAL.replace("bands:", "bands:\n  team-ghost: [40, 50]"))

    def test_project_cannot_be_both_team_and_ungoverned(self):
        with pytest.raises(Exception, match="both a team and ungoverned"):
            parse(
                MINIMAL.replace("ungoverned_projects: [main]", "ungoverned_projects: [team-infra]")
            )

    def test_unknown_day_rejected(self):
        with pytest.raises(Exception, match="unknown day"):
            parse(MINIMAL.replace("days: [mon, tue, wed, thu, fri]", "days: [funday]"))

    def test_bad_time_rejected(self):
        with pytest.raises(Exception, match="not a valid time"):
            parse(MINIMAL.replace('to: "20:00"', 'to: "25:00"'))

    def test_equal_window_bounds_rejected(self):
        with pytest.raises(Exception, match="empty window"):
            parse(MINIMAL.replace('to: "20:00"', 'to: "08:00"'))

    def test_unknown_timezone_rejected(self):
        with pytest.raises(Exception, match="unknown timezone"):
            parse(MINIMAL.replace("timezone: UTC", "timezone: Mars/Olympus"))

    def test_unsupported_version_rejected(self):
        with pytest.raises(Exception, match="unsupported policy version"):
            parse(MINIMAL.replace("version: 1", "version: 2"))

    def test_empty_backends_rejected(self):
        with pytest.raises(Exception, match="would forbid every option"):
            parse(MINIMAL.replace("backends: [nebius]", "backends: []"))


class TestResolve:
    def test_user_override_wins(self):
        config = parse(MINIMAL)
        policy = policy_config.resolve(config, "team-research", "alice")
        assert policy.max_run_duration == 24 * 3600

    def test_unset_keys_inherit_team_defaults(self):
        config = parse(MINIMAL)
        policy = policy_config.resolve(config, "team-research", "alice")
        assert policy.windows == config.teams["team-research"].defaults.windows
        assert policy.cloud.allowed is True

    def test_empty_override_inherits_everything(self):
        config = parse(MINIMAL)
        policy = policy_config.resolve(config, "team-research", "carol")
        assert policy.max_run_duration == 12 * 3600
        assert policy.cloud.max_price == 4.0

    def test_cloud_merges_key_by_key(self):
        """Overriding `allowed` must not drop the team's other cloud limits.

        Wholesale replacement here would silently widen bob's access by dropping
        `max_price` and `backends` — the failure mode worth a test.
        """
        config = parse(MINIMAL)
        policy = policy_config.resolve(config, "team-research", "bob")
        assert policy.cloud.allowed is False
        assert policy.cloud.max_price == 4.0
        assert policy.cloud.backends == ["nebius"]

    def test_resolve_does_not_mutate_the_shared_defaults(self):
        config = parse(MINIMAL)
        policy_config.resolve(config, "team-research", "alice")
        assert config.teams["team-research"].defaults.max_run_duration == 12 * 3600

    def test_unlisted_user_is_denied(self):
        config = parse(MINIMAL)
        with pytest.raises(ValueError, match="No policy entry for user 'mallory'"):
            policy_config.resolve(config, "team-research", "mallory")

    def test_denial_names_the_team_and_the_file(self):
        config = parse(MINIMAL)
        with pytest.raises(ValueError) as excinfo:
            policy_config.resolve(config, "team-research", "mallory")
        assert "team-research" in str(excinfo.value)
        assert "policy.yaml" in str(excinfo.value)


class TestLoader:
    def test_loads_from_the_env_var(self, write_policy):
        write_policy(MINIMAL)
        assert set(policy_config.load().teams) == {"team-research", "team-infra"}

    def test_caches_while_the_file_is_unchanged(self, write_policy):
        write_policy(MINIMAL)
        assert policy_config.load() is policy_config.load()

    def test_reloads_after_an_edit(self, write_policy):
        path = write_policy(MINIMAL)
        first = policy_config.load()
        path.write_text(MINIMAL.replace("max_run_duration: 4h", "max_run_duration: 8h"))
        second = policy_config.load()
        assert second is not first
        assert second.teams["team-infra"].defaults.max_run_duration == 8 * 3600

    def test_missing_file_raises_rather_than_defaulting_open(self, tmp_path, monkeypatch):
        monkeypatch.setenv(policy_config.POLICY_FILE_ENV_VAR, str(tmp_path / "absent.yaml"))
        with pytest.raises(ValueError, match="Cannot read the policy file"):
            policy_config.load()

    def test_malformed_file_raises_rather_than_serving_the_stale_parse(self, write_policy):
        path = write_policy(MINIMAL)
        policy_config.load()
        path.write_text("version: 1\nbands: [not, a, mapping]\n")
        with pytest.raises(ValueError, match="is invalid"):
            policy_config.load()
