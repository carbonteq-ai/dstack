"""The apply policy: admission control and spec clamping.

dstack calls `on_apply` twice per `dstack apply` — once for the plan and once for
the apply — passing the original spec both times. Only the apply call persists;
the plan call is what renders the clamped values in the CLI's plan table before
the user confirms.

Raising `ValueError` is the rejection mechanism: dstack turns it into a
`ServerClientError` carrying our message, which the CLI prints verbatim. The
messages below are therefore end-user text, not log lines.

Clamps are read from `spec.merged_profile` and written to `spec.configuration`.
That asymmetry is deliberate. `merged_profile` is what the run actually asked
for once `profile:` and the configuration have been combined, so reading it
avoids widening a limit the user set in the profile; writing to `configuration`
makes our value win, because dstack rebuilds `merged_profile` from the spec we
return and configuration values override profile values there.

Order matters: the cloud rules run before the duration clamp, because a run that
has been pinned to the on-prem fleet cannot spend money and so must not have its
duration shortened by a dollar budget.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

from dstack.plugins import (
    ApplyPolicy,
    FleetSpec,
    GatewaySpec,
    Plugin,
    RunSpec,
    VolumeSpec,
    get_plugin_logger,
)

from ctpolicy import config as policy_config
from ctpolicy import usage as usage_module
from ctpolicy import windows
from ctpolicy._compat import BackendType
from ctpolicy.config import PolicyConfig, PolicySpec, TeamConfig
from ctpolicy.usage import Snapshot, SnapshotUnavailable

logger = get_plugin_logger(__name__)

SECONDS_PER_HOUR = 3600.0


def _now(tz) -> datetime:
    """Wall clock, isolated so tests can pin an instant."""
    return datetime.now(tz)


@dataclass
class _Budgets:
    """What is left of each budget that applies to this submission.

    `None` means the budget is not configured, which is different from zero.
    `scope` names whichever of the team or the user is the binding one, so a
    rejection can say who ran out.
    """

    seconds: Optional[float] = None
    seconds_scope: str = ""
    dollars: Optional[float] = None
    dollars_scope: str = ""


class CtPolicy(ApplyPolicy):
    def on_run_apply(self, user: str, project: str, spec: RunSpec) -> RunSpec:
        config = policy_config.load()
        if _is_ungoverned(config, project):
            return spec
        team_config = config.teams[project]
        policy = _resolve_or_reject(config, project, user)

        now = _now(config.tz)
        window_close = _check_window(policy, user, project, now, config)
        budgets = _remaining_budgets(config, team_config, project, user, now)

        _apply_cloud_rules(spec, policy, budgets, project, user)
        ceiling = _duration_ceiling(spec, policy, budgets, now, window_close)
        _clamp_max_duration(spec, ceiling)
        _assign_priority(spec, config, project)
        return spec

    def on_fleet_apply(self, user: str, project: str, spec: FleetSpec) -> FleetSpec:
        config = policy_config.load()
        if _is_ungoverned(config, project):
            return spec
        policy = _resolve_or_reject(config, project, user)
        # dstack itself distinguishes the two kinds of fleet this way; see the
        # "Can only export SSH fleets" check in the server's exports service.
        is_cloud_fleet = spec.configuration.ssh_config is None
        if is_cloud_fleet and not _cloud_allowed(policy):
            logger.warning("User %s tried to create a cloud fleet in %s", user, project)
            raise ValueError(
                f"Team {project!r} may not provision external cloud resources,"
                f" and a fleet without `ssh_config` is a cloud fleet."
                f" Add `ssh_config` to enroll on-prem hosts instead."
            )
        return spec

    def on_volume_apply(self, user: str, project: str, spec: VolumeSpec) -> VolumeSpec:
        self._reject_cloud_resource(user, project, "volume")
        return spec

    def on_gateway_apply(self, user: str, project: str, spec: GatewaySpec) -> GatewaySpec:
        self._reject_cloud_resource(user, project, "gateway")
        return spec

    def _reject_cloud_resource(self, user: str, project: str, kind: str) -> None:
        config = policy_config.load()
        if _is_ungoverned(config, project):
            return
        policy = _resolve_or_reject(config, project, user)
        if not _cloud_allowed(policy):
            logger.warning("User %s tried to create a %s in %s", user, kind, project)
            raise ValueError(
                f"Team {project!r} may not provision external cloud resources,"
                f" and a {kind} is backed by one."
            )


class PolicyPlugin(Plugin):
    def get_apply_policies(self) -> list[ApplyPolicy]:
        return [CtPolicy()]


def _is_ungoverned(config: PolicyConfig, project: str) -> bool:
    """Whether this project is deliberately outside the policy layer.

    Only projects an admin listed are exempt. Anything else that is not a team is
    rejected, so adding a project can never quietly create an ungoverned team.
    """
    if project in config.ungoverned_projects:
        return True
    if project not in config.teams:
        raise ValueError(
            f"Project {project!r} has no policy. An admin must either add it to"
            f" `teams` in policy.yaml or list it under `ungoverned_projects`."
        )
    return False


def _resolve_or_reject(config: PolicyConfig, project: str, user: str) -> PolicySpec:
    try:
        return policy_config.resolve(config, project, user)
    except ValueError:
        logger.warning("User %s has no policy entry in team %s", user, project)
        raise


def _cloud_allowed(policy: PolicySpec) -> bool:
    return policy.cloud is not None and policy.cloud.allowed


def _check_window(
    policy: PolicySpec, user: str, project: str, now: datetime, config: PolicyConfig
) -> Optional[datetime]:
    """Reject if the team is outside its compute window; else say when it closes.

    Returns `None` when the team has no window configured, meaning always open.
    """
    if policy.windows is None:
        return None
    intervals = windows.materialize(policy.windows, config.tz, now)
    if windows.is_open(intervals, now):
        return windows.current_close(intervals, now)

    allowed = "; ".join(w.pretty() for w in policy.windows)
    opens = windows.next_open(intervals, now)
    when = f" Next window opens {opens:%a %H:%M}." if opens is not None else ""
    logger.warning("User %s submitted outside the %s window", user, project)
    raise ValueError(
        f"Outside the {project} compute window."
        f" Allowed: {allowed} {config.timezone}. Now: {now:%a %H:%M}.{when}"
    )


def _remaining_budgets(
    config: PolicyConfig,
    team_config: TeamConfig,
    project: str,
    user: str,
    now: datetime,
) -> _Budgets:
    """How much of each budget is left, across the team and the user.

    The team budget is the team's own `defaults`, measured against the whole
    team's usage. A user budget is only the one written under that user, and is
    measured against their usage alone — so a user override carves a slice out of
    the team pool rather than replacing it. Both must have room, and the tighter
    one is what bounds the run.

    `committed` is subtracted as well as spent. Without it, several runs each
    individually under budget could collectively exceed it.
    """
    team_spec = team_config.defaults
    user_spec = team_config.users.get(user) or PolicySpec()

    if not (team_spec.needs_usage() or user_spec.needs_usage()):
        # No budget anywhere for this user, so the snapshot is irrelevant. This
        # is what stops a dead enforcer from taking down teams that configure no
        # budgets at all.
        return _Budgets()

    snapshot = _load_snapshot(config, project, user, now)
    if snapshot is None:
        return _Budgets()

    budgets = _Budgets()
    for scope_label, spec, scope_user in (
        (f"Team {project!r}", team_spec, None),
        (f"User {user!r}", user_spec, user),
    ):
        scope = snapshot.scope(project, scope_user)

        if spec.time_budget is not None:
            period = spec.time_budget.period
            used = scope.usage_for(period)
            left = spec.time_budget.limit - used.seconds - scope.committed.seconds
            if left <= 0:
                logger.warning("%s is out of time budget in %s", scope_label, project)
                raise ValueError(
                    f"{scope_label} has no time budget left for"
                    f" {usage_module.period_label(period, now, config.tz)}:"
                    f" {usage_module.format_duration(spec.time_budget.limit)} allowed,"
                    f" {usage_module.format_duration(used.seconds)} already used and"
                    f" {usage_module.format_duration(scope.committed.seconds)} committed by"
                    f" runs still going."
                    f" Wait for the period to roll over, or ask an admin to raise it."
                )
            if budgets.seconds is None or left < budgets.seconds:
                budgets.seconds, budgets.seconds_scope = left, scope_label

        cloud = spec.cloud
        if cloud is not None and cloud.dollar_budget is not None:
            period = cloud.dollar_budget.period
            used = scope.usage_for(period)
            left = cloud.dollar_budget.limit - used.dollars - scope.committed.dollars
            if budgets.dollars is None or left < budgets.dollars:
                budgets.dollars, budgets.dollars_scope = left, scope_label
    return budgets


def _load_snapshot(
    config: PolicyConfig, project: str, user: str, now: datetime
) -> Optional[Snapshot]:
    """Read the usage snapshot, applying `on_usage_unavailable` when it is not there.

    Reached only when a budget is actually configured for this user, so a missing
    snapshot cannot affect teams that have none.

    Staleness is measured against the same clock the window rules use, rather
    than reading the wall clock again, so every decision in one admission is
    taken at a single instant.
    """
    try:
        return usage_module.load(max_age=float(config.usage_snapshot_max_age), now=now)
    except SnapshotUnavailable as e:
        if config.on_usage_unavailable == "allow":
            logger.warning(
                "Usage snapshot unavailable (%s); admitting %s/%s because"
                " on_usage_unavailable is 'allow'",
                e,
                project,
                user,
            )
            return None
        logger.error("Usage snapshot unavailable (%s); denying %s/%s", e, project, user)
        raise ValueError(
            f"Budget usage is currently unknown, so {project!r} cannot admit runs: {e}."
            f" This clears once the policy enforcer refreshes it; tell an admin if"
            f" it persists."
        )


def _duration_ceiling(
    spec: RunSpec,
    policy: PolicySpec,
    budgets: _Budgets,
    now: datetime,
    window_close: Optional[datetime],
) -> Optional[int]:
    """The tightest bound on how long this run may occupy compute.

    Combines the team's `max_run_duration`, the time left in the window, and
    whatever the budgets still afford. `None` means unbounded.
    """
    ceiling: Optional[float] = policy.max_run_duration

    if window_close is not None:
        remaining = max(0.0, (window_close - now).total_seconds())
        ceiling = remaining if ceiling is None else min(ceiling, remaining)

    if budgets.seconds is not None:
        ceiling = budgets.seconds if ceiling is None else min(ceiling, budgets.seconds)

    # A dollar budget only bounds duration for a run that can actually reach a
    # paid backend. Applying it to an on-prem-pinned run would shorten it over
    # money it can never spend, since dstack prices SSH instances at zero.
    if budgets.dollars is not None and not _pinned_on_prem(spec):
        # `_effective`, not `_merged`: the cloud rules have already run and may
        # have written the team's ceiling into the configuration, and
        # `merged_profile` is a parse-time snapshot that does not see it.
        price = _effective(spec, "max_price")
        if price:
            affordable = max(0.0, budgets.dollars) / price * SECONDS_PER_HOUR
            ceiling = affordable if ceiling is None else min(ceiling, affordable)

    if ceiling is None:
        return None
    return max(0, int(ceiling))


def _clamp_max_duration(spec: RunSpec, ceiling: Optional[int]) -> None:
    """Apply the ceiling to the run's `max_duration`.

    Enforcement is the runner's: it starts this timer in the VM when the job
    starts running, so the bound holds on the SSH fleet and even if the server is
    unreachable. Because that clock excludes provisioning, a run can still
    outlive a window by roughly its provisioning time — the enforcer covers that
    residue.
    """
    if ceiling is None:
        return
    requested = _merged(spec, "max_duration")
    # `None` means dstack's default and "off" means unlimited; both lose to a
    # ceiling. Only an explicit, tighter request survives.
    if isinstance(requested, int) and requested <= ceiling:
        return
    spec.configuration.max_duration = ceiling


def _apply_cloud_rules(
    spec: RunSpec, policy: PolicySpec, budgets: _Budgets, project: str, user: str
) -> None:
    requested_backends = _merged(spec, "backends")
    cloud_allowed = _cloud_allowed(policy)

    # An exhausted dollar budget does not stop a team working, it stops them
    # spending. The run falls back to the on-prem fleet, which costs nothing.
    if cloud_allowed and budgets.dollars is not None and budgets.dollars <= 0:
        cloud_requested = _cloud_backends(requested_backends)
        if cloud_requested:
            names = ", ".join(sorted(b.value for b in cloud_requested))
            logger.warning("%s is out of cloud budget in %s", budgets.dollars_scope, project)
            raise ValueError(
                f"{budgets.dollars_scope} has no cloud budget left, but this run"
                f" requests: {names}. Remove `backends` to run on the on-prem"
                f" fleet, which does not consume the budget."
            )
        logger.info("Pinning %s/%s to on-prem: cloud budget exhausted", project, user)
        spec.configuration.backends = [BackendType.REMOTE]
        return

    if not cloud_allowed:
        cloud_requested = _cloud_backends(requested_backends)
        if cloud_requested:
            names = ", ".join(sorted(b.value for b in cloud_requested))
            logger.warning("User %s requested cloud backends %s in %s", user, names, project)
            raise ValueError(
                f"Team {project!r} may not provision external cloud resources,"
                f" but this run requests: {names}."
                f" Remove `backends` to run on the on-prem fleet."
            )
        # Pin rather than leave unset, so the run cannot reach a cloud backend
        # that is later added to the project.
        spec.configuration.backends = [BackendType.REMOTE]
        return

    if policy.cloud is not None and policy.cloud.backends is not None:
        # The on-prem fleet stays reachable regardless: the allowlist governs
        # which *cloud* backends a team may use, not whether it may run at all.
        allowed = {BackendType(b) for b in policy.cloud.backends} | {BackendType.REMOTE}
        if requested_backends:
            permitted = [b for b in requested_backends if b in allowed]
            if not permitted:
                names = ", ".join(sorted(b.value for b in requested_backends))
                allowed_names = ", ".join(sorted(b.value for b in allowed))
                raise ValueError(
                    f"Team {project!r} may not use backends: {names}. Allowed: {allowed_names}."
                )
            spec.configuration.backends = permitted
        else:
            spec.configuration.backends = sorted(allowed, key=lambda b: b.value)

    if policy.cloud is not None and policy.cloud.max_price is not None:
        requested_price = _merged(spec, "max_price")
        ceiling = policy.cloud.max_price
        if requested_price is None or requested_price > ceiling:
            spec.configuration.max_price = ceiling


def _cloud_backends(backends) -> List[BackendType]:
    return [b for b in backends or [] if b != BackendType.REMOTE]


def _pinned_on_prem(spec: RunSpec) -> bool:
    backends = _effective(spec, "backends")
    if not backends:
        return False
    return all(b == BackendType.REMOTE for b in backends)


def _assign_priority(spec: RunSpec, config: PolicyConfig, project: str) -> None:
    """Pack the team's priority and the run's own priority into dstack's one field.

    dstack orders the submitted-job queue by a single `priority` integer, and that
    ordering is global across projects. Giving each team a disjoint sub-range
    therefore makes any run of a higher-priority team sort ahead of every run of a
    lower-priority one, while the run's own 0-100 priority only orders runs within
    its team. The user's value is mapped proportionally into the band so nobody
    has to know how wide their band is.

    The mapping is not idempotent — re-mapping an already-banded value moves it up
    within the band — and it does not need to be. `ApplyPolicy.on_apply` documents
    that the original spec is passed on both the plan and the apply call, and the
    client cannot feed our output back: `ApplyRunPlanInput` carries only
    `run_spec`, which is the untouched original. The invariant that matters holds
    regardless of how many times this runs: the result is always inside the team's
    band, so no run can ever outrank a higher-priority team's runs. In-place
    updates re-enter this hook too, so a hand-edited priority is re-banded.
    """
    low, high = config.band(project)
    width = high - low + 1
    requested = spec.configuration.priority
    if requested is None:
        requested = policy_config.PRIORITY_MIN
    # Floor rather than round: monotonic, exact at both ends (0 -> low,
    # 100 -> high), and free of round-half-to-even surprises mid-band.
    offset = requested * (width - 1) // policy_config.PRIORITY_MAX
    spec.configuration.priority = low + offset


def _merged(spec: RunSpec, field: str):
    """Read a profile parameter as the run originally requested it.

    `merged_profile` combines `profile:` and `configuration:`, so this is the
    value to compare a limit against — reading the configuration alone would
    miss a tighter value the user set in their profile and widen it.

    It is a parse-time snapshot: it does not reflect anything this module has
    written to `spec.configuration` during the same call. Use `_effective` for
    that. `merged_profile` is also typed optional, so fall back rather than
    crash the apply.
    """
    profile = getattr(spec, "merged_profile", None)
    if profile is None:
        return getattr(spec.configuration, field, None)
    return getattr(profile, field, None)


def _effective(spec: RunSpec, field: str):
    """Read a parameter as it now stands, including our own clamps.

    dstack rebuilds `merged_profile` from the spec we return, with configuration
    winning over profile, so reading the configuration first is what the server
    will end up seeing.
    """
    value = getattr(spec.configuration, field, None)
    if value is not None:
        return value
    return _merged(spec, field)
