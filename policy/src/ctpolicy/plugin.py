"""The apply policy: admission control and spec clamping.

dstack calls `on_apply` twice per `dstack apply` — once for the plan and once for
the apply — passing the original spec both times, so every rule here is written
to be idempotent. Only the apply call persists; the plan call is what renders the
clamped values in the CLI's plan table before the user confirms.

Raising `ValueError` is the rejection mechanism: dstack turns it into a
`ServerClientError` carrying our message, which the CLI prints verbatim. The
messages below are therefore end-user text, not log lines.

Clamps are read from `spec.merged_profile` and written to `spec.configuration`.
That asymmetry is deliberate. `merged_profile` is what the run actually asked
for once `profile:` and the configuration have been combined, so reading it
avoids widening a limit the user set in the profile; writing to `configuration`
makes our value win, because dstack rebuilds `merged_profile` from the spec we
return and configuration values override profile values there.
"""

from datetime import datetime
from typing import Optional

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
from ctpolicy import windows
from ctpolicy._compat import BackendType
from ctpolicy.config import PolicyConfig, PolicySpec

logger = get_plugin_logger(__name__)


def _now(tz) -> datetime:
    """Wall clock, isolated so tests can pin an instant."""
    return datetime.now(tz)


class CtPolicy(ApplyPolicy):
    def on_run_apply(self, user: str, project: str, spec: RunSpec) -> RunSpec:
        config = policy_config.load()
        if _is_ungoverned(config, project):
            return spec
        policy = _resolve_or_reject(config, project, user)

        now = _now(config.tz)
        window_close = _check_window(policy, user, project, now, config)
        _clamp_max_duration(spec, policy, now, window_close)
        _apply_cloud_rules(spec, policy, project, user)
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


def _clamp_max_duration(
    spec: RunSpec, policy: PolicySpec, now: datetime, window_close: Optional[datetime]
) -> None:
    """Bound how long the run may occupy compute.

    The ceiling is the tighter of the team's `max_run_duration` and the time left
    in the window. Enforcement is the runner's: it starts this timer in the VM
    when the job starts running, so the bound holds on the SSH fleet and even if
    the server is unreachable. Because that clock excludes provisioning, a run
    can still outlive the window by roughly its provisioning time — the enforcer
    covers that residue.
    """
    ceiling = policy.max_run_duration
    if window_close is not None:
        remaining = max(0, int((window_close - now).total_seconds()))
        ceiling = remaining if ceiling is None else min(ceiling, remaining)
    if ceiling is None:
        return

    requested = _merged(spec, "max_duration")
    # `None` means dstack's default and "off" means unlimited; both lose to a
    # ceiling. Only an explicit, tighter request survives.
    if isinstance(requested, int) and requested <= ceiling:
        return
    spec.configuration.max_duration = ceiling


def _apply_cloud_rules(spec: RunSpec, policy: PolicySpec, project: str, user: str) -> None:
    requested_backends = _merged(spec, "backends")

    if not _cloud_allowed(policy):
        cloud_requested = [b for b in requested_backends or [] if b != BackendType.REMOTE]
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
    """Read a profile parameter as the run effectively requested it.

    `merged_profile` is populated by a RunSpec validator, but it is typed as
    optional, so fall back to the configuration rather than crash the apply.
    """
    profile = getattr(spec, "merged_profile", None)
    if profile is None:
        return getattr(spec.configuration, field, None)
    return getattr(profile, field, None)
