"""policy.yaml: schema, validation, merging, and a mtime-cached loader.

The file is the only source of policy. Nothing is stored in a database and no
decision depends on state this package owns, so a policy edit takes effect on the
next `dstack apply` without a server restart — unlike server/config.yml, which
dstack applies once at boot.

Parsing is strict (`extra = "forbid"`) and failures are not swallowed: a
malformed file makes every admission raise, which rejects submissions with the
parse error rather than silently falling back to a stale or permissive policy.
"""

import os
import re
import threading
from enum import Enum
from pathlib import Path
from typing import Dict, FrozenSet, List, Literal, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, Field, root_validator, validator

from ctpolicy._compat import Duration
from ctpolicy.windows import DAY_NAMES, SECONDS_PER_DAY

DEFAULT_POLICY_FILE = "/etc/ctpolicy/policy.yaml"
POLICY_FILE_ENV_VAR = "DSTACK_CT_POLICY_FILE"

PRIORITY_MIN = 0
PRIORITY_MAX = 100
"""Mirrors RUN_PRIOTIRY_MIN/MAX in dstack's run configuration model. dstack
validates the field itself, so a band outside this range would be rejected far
from its cause; the band validator catches it at load instead."""

_TIME_RE = re.compile(r"^(?P<hours>\d{1,2}):(?P<minutes>\d{2})$")


class _Model(BaseModel):
    class Config:
        extra = "forbid"
        allow_population_by_field_name = True


class Window(_Model):
    """A recurring local-time compute window, e.g. `Mon-Fri 08:00-20:00`.

    Stored as seconds from midnight rather than `time` objects so that `24:00` —
    which is not a valid `time` — is representable as an end.
    """

    weekdays: FrozenSet[int] = Field(alias="days")
    start_seconds: int = Field(alias="from")
    end_seconds: int = Field(alias="to")

    @validator("weekdays", pre=True)
    def _parse_days(cls, v):
        if not isinstance(v, list) or not v:
            raise ValueError("days must be a non-empty list, e.g. [mon, tue, wed, thu, fri]")
        days = set()
        for name in v:
            key = str(name).strip().lower()
            if key not in DAY_NAMES:
                raise ValueError(f"unknown day {name!r}; expected one of {', '.join(DAY_NAMES)}")
            days.add(DAY_NAMES.index(key))
        return frozenset(days)

    @validator("start_seconds", "end_seconds", pre=True)
    def _parse_time(cls, v):
        match = _TIME_RE.match(str(v).strip())
        if match is None:
            raise ValueError(f"expected a HH:MM time, got {v!r}")
        hours, minutes = int(match.group("hours")), int(match.group("minutes"))
        if minutes > 59 or hours > 24 or (hours == 24 and minutes != 0):
            raise ValueError(f"{v!r} is not a valid time (00:00-24:00)")
        return hours * 3600 + minutes * 60

    @validator("end_seconds")
    def _reject_empty(cls, v, values):
        # Equal bounds are almost certainly a typo: read as a wrap-around window
        # it would mean "always open", which is the opposite of what someone
        # writing `from: 08:00, to: 08:00` intends.
        if v == values.get("start_seconds"):
            raise ValueError(
                "`from` and `to` are equal, which defines an empty window."
                " Use `to: 24:00` for the end of the day."
            )
        return v

    def pretty(self) -> str:
        """Render the window the way the rejection message shows it."""
        names = "/".join(DAY_NAMES[d] for d in sorted(self.weekdays))
        return f"{names} {_fmt_seconds(self.start_seconds)}-{_fmt_seconds(self.end_seconds)}"


def _fmt_seconds(seconds: int) -> str:
    if seconds == SECONDS_PER_DAY:
        return "24:00"
    return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}"


class Period(str, Enum):
    """The window a budget resets on, evaluated in the config timezone."""

    MONTH = "month"
    WEEK = "week"


class TimeBudget(_Model):
    period: Period
    limit: Duration
    """Seconds of run time per period, written as e.g. `400h`."""

    @validator("limit")
    def _positive(cls, v):
        if v <= 0:
            raise ValueError("time_budget.limit must be positive")
        return v


class DollarBudget(_Model):
    period: Period
    limit: float = Field(gt=0.0)
    """Dollars per period. Only cloud spend counts against it: dstack prices
    SSH/on-prem instances at zero, so on-prem usage cannot consume it."""


class CloudPolicy(_Model):
    """Whether and how a team or user may provision external cloud resources.

    `allowed` is the boolean gate. The rest only narrows what an allowed team may
    ask for; dstack's own profile fields carry the same names and meanings.
    """

    allowed: bool = False
    dollar_budget: Optional[DollarBudget] = None
    max_price: Optional[float] = Field(default=None, gt=0.0)
    """Ceiling on instance price in dollars per hour, clamping `max_price`."""
    backends: Optional[List[str]] = None
    """Allowlist of *cloud* backends, intersected with the run's request.

    There is deliberately no `regions` counterpart. Regions would have to be
    applied to the same spec field that the shared on-prem fleet is matched
    through, so a cloud-oriented region list could quietly make a team unable to
    run on-prem at all. Cost is already bounded by `max_price` and the budgets.
    """

    @validator("backends")
    def _reject_empty_list(cls, v):
        if v is not None and not v:
            raise ValueError(
                "`backends: []` would forbid every option."
                " Omit the key to leave it unrestricted, or set `allowed: false`."
            )
        return v


class PolicySpec(_Model):
    """The policy body shared by a team's `defaults` and a user's override.

    Every field is optional so a user override can set one key without restating
    the rest; `merge` resolves the two.
    """

    windows: Optional[List[Window]] = None
    max_run_duration: Optional[Duration] = None
    time_budget: Optional[TimeBudget] = None
    cloud: Optional[CloudPolicy] = None

    def needs_usage(self) -> bool:
        """Whether enforcing this policy requires the usage snapshot.

        A policy with no budget is decided entirely from the spec and the clock,
        so it must keep working when the snapshot is missing — otherwise adding
        budgets to one team would take every other team down with the enforcer.
        """
        return self.time_budget is not None or (
            self.cloud is not None and self.cloud.dollar_budget is not None
        )

    @validator("windows")
    def _reject_empty_windows(cls, v):
        if v is not None and not v:
            raise ValueError(
                "`windows: []` would never be open."
                " Omit the key to inherit, or give at least one window."
            )
        return v

    @validator("max_run_duration")
    def _reject_nonpositive(cls, v):
        if v is not None and v <= 0:
            raise ValueError("max_run_duration must be positive")
        return v


class TeamConfig(_Model):
    defaults: PolicySpec = PolicySpec()
    users: Dict[str, PolicySpec] = {}
    """A user must be listed here to run at all; `alice: {}` grants the team
    defaults unchanged. Absence is denied rather than defaulted so that a
    forgotten entry cannot silently grant budget."""

    @validator("users", pre=True)
    def _normalize_users(cls, v):
        if not isinstance(v, dict):
            raise ValueError("users must be a mapping of username to overrides")
        # `alice:` with nothing after it parses as None; treat it as `alice: {}`.
        return {name: (spec if spec is not None else {}) for name, spec in v.items()}


class PolicyConfig(_Model):
    version: int
    timezone: str = "UTC"
    on_usage_unavailable: Literal["deny", "allow"] = "deny"
    """What to do when a budget must be checked but the usage snapshot is
    missing or stale. Defaults to denying: a quota layer that opens up when its
    inputs disappear is not a quota layer. Only affects policies that actually
    configure a budget."""
    usage_snapshot_max_age: Duration = 180
    """How old the snapshot may be before it counts as unavailable. Must exceed
    the enforcer's refresh interval or every submission fails between cycles."""
    ungoverned_projects: List[str] = []
    """Projects that carry no team policy, typically the infra project that owns
    the shared fleet. Listing one is an explicit admin act: a project that is
    neither a team nor listed here is denied, so creating a project can never
    silently create an ungoverned team."""
    bands: Dict[str, Tuple[int, int]] = {}
    teams: Dict[str, TeamConfig] = {}

    @validator("version")
    def _check_version(cls, v):
        if v != 1:
            raise ValueError(f"unsupported policy version {v}; this build understands version 1")
        return v

    @validator("timezone")
    def _check_timezone(cls, v):
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError(f"unknown timezone {v!r}: {e}")
        return v

    @validator("bands")
    def _check_bands(cls, v):
        seen: List[Tuple[int, int, str]] = []
        for team, (low, high) in v.items():
            if low > high:
                raise ValueError(f"band for {team!r} is inverted: [{low}, {high}]")
            if low < PRIORITY_MIN or high > PRIORITY_MAX:
                raise ValueError(
                    f"band for {team!r} is [{low}, {high}],"
                    f" outside dstack's priority range {PRIORITY_MIN}-{PRIORITY_MAX}"
                )
            seen.append((low, high, team))
        seen.sort()
        for (low, high, team), (next_low, _, next_team) in zip(seen, seen[1:]):
            if next_low <= high:
                raise ValueError(
                    f"bands for {team!r} [{low}, {high}] and {next_team!r} overlap."
                    " Overlapping bands would let a low-priority team outrank a high-priority one."
                )
        return v

    @validator("teams")
    def _check_teams_have_bands(cls, v, values):
        bands = values.get("bands")
        if bands is None:
            return v  # the band validator already failed; do not pile on
        if missing := sorted(set(v) - set(bands)):
            raise ValueError(f"teams with no priority band: {', '.join(missing)}")
        if extra := sorted(set(bands) - set(v)):
            raise ValueError(f"bands for teams that do not exist: {', '.join(extra)}")
        return v

    @root_validator(skip_on_failure=True)
    def _check_not_also_a_team(cls, values):
        # A root validator rather than a field validator: pydantic v1 runs field
        # validators in declaration order, and `ungoverned_projects` is declared
        # before `teams`, so `values["teams"]` would not be populated yet.
        ungoverned = set(values.get("ungoverned_projects") or [])
        if both := sorted(ungoverned & set(values.get("teams") or {})):
            raise ValueError(f"projects listed as both a team and ungoverned: {', '.join(both)}")
        return values

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def band(self, team: str) -> Tuple[int, int]:
        return self.bands[team]


def merge(defaults: PolicySpec, override: PolicySpec) -> PolicySpec:
    """Resolve a user override against their team's defaults.

    The user value wins for every key that they set, which is what "user-level
    policies override team-level ones" means. `cloud` merges key by key rather
    than wholesale, so overriding one cloud setting does not silently drop the
    team's other cloud restrictions — the failure mode that would matter most,
    since dropping `backends` or `max_price` widens rather than narrows.
    """
    merged = defaults.copy(deep=True)
    for name in ("windows", "max_run_duration", "time_budget"):
        value = getattr(override, name)
        if value is not None:
            setattr(merged, name, value)
    if override.cloud is not None:
        if merged.cloud is None:
            merged.cloud = override.cloud.copy(deep=True)
        else:
            set_keys = override.cloud.__fields_set__
            for name in set_keys:
                setattr(merged.cloud, name, getattr(override.cloud, name))
    return merged


def resolve(config: PolicyConfig, team: str, user: str) -> PolicySpec:
    """The effective policy for one user in one team.

    Raises `ValueError` with a message meant for the submitting user; the plugin
    lets it propagate so dstack turns it into the CLI's rejection text.
    """
    team_config = config.teams[team]
    override = team_config.users.get(user)
    if override is None:
        known = ", ".join(sorted(team_config.users)) or "(none)"
        raise ValueError(
            f"No policy entry for user {user!r} in team {team!r}."
            f" Ask an admin to add you to policy.yaml."
            f" Users currently configured for this team: {known}."
        )
    return merge(team_config.defaults, override)


_cache_lock = threading.Lock()
_cache: Optional[Tuple[Tuple[str, int, int], PolicyConfig]] = None


def policy_file_path() -> Path:
    return Path(os.getenv(POLICY_FILE_ENV_VAR, DEFAULT_POLICY_FILE))


def load(path: Optional[Path] = None) -> PolicyConfig:
    """Parse policy.yaml, reusing the last parse while the file is unchanged.

    Keyed on mtime and size so an edit is picked up on the next apply without a
    server restart. The hook runs on a shared server executor thread, so the hot
    path must stay a stat plus a dict lookup.
    """
    path = path or policy_file_path()
    try:
        stat = path.stat()
    except OSError as e:
        raise ValueError(f"Cannot read the policy file at {path}: {e}")
    key = (str(path), stat.st_mtime_ns, stat.st_size)

    global _cache
    with _cache_lock:
        if _cache is not None and _cache[0] == key:
            return _cache[1]
        config = _parse(path)
        _cache = (key, config)
        return config


def _parse(path: Path) -> PolicyConfig:
    try:
        raw = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as e:
        raise ValueError(f"Cannot parse the policy file at {path}: {e}")
    if not isinstance(raw, dict):
        raise ValueError(f"The policy file at {path} must contain a YAML mapping")
    try:
        return PolicyConfig.parse_obj(raw)
    except Exception as e:
        raise ValueError(f"The policy file at {path} is invalid: {e}")


def clear_cache() -> None:
    """Drop the parsed-config cache. For tests."""
    global _cache
    with _cache_lock:
        _cache = None
