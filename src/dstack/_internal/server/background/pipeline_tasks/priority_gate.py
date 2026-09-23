"""
CarbonTeq delta: the priority gate at placement (control plane ADR-046, S-29).

Upstream reads `RunModel.priority` in exactly one place, the submitted-jobs fetch's
`ORDER BY`. On a fleet that is not saturated that ordering decides almost nothing: a
new job is fetched alone, a shared batch is placed by concurrent workers racing for
the instance lock, and a run waiting for capacity retries on its own jittered timer.
Measured on the CarbonTeq deployment, a priority-29 run took a host a waiting
priority-99 run fitted, including from a same-second batch in priority order.

This module answers one question for a job about to take an existing instance: is a
strictly higher-priority run waiting that fits one of those instances? If so, the job
yields — it is deferred, not failed, and uses no retry attempt.

What it deliberately does not do:

- **Head-of-line blocking.** A higher run that fits none of the offered instances
  blocks nobody. The fleet is never idled for a job that cannot use the slot.
- **Order within a priority.** Equal priority never yields.
- **New capacity.** Only existing-instance offers are gated. Provisioning is a market,
  not a slot.
- **Held runs.** A run held for a window or a schedule (`PENDING` with
  `resubmission_attempt == 0`) is not waiting, or a closed window would block the fleet.

It is bounded by the yielding run's own no-capacity retry window, so a deferred job
cannot wait forever, and it fails open: any error means no yield.

**Fit before count** (control plane ADR-049, decision 2). The gate once kept the ten
highest waiters and only then checked fit, so ten higher waiters that fit nothing, such
as `spot` runs beside an on-demand instance, hid an eleventh that fitted, and a lower run
took its slot. Every waiting candidate is now fit-checked in priority order and the first
fit yields. The work is bounded by the candidate fetch, not by a count of waiters.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import selectinload

from dstack._internal.core.models.common import EntityReference
from dstack._internal.core.models.instances import InstanceOfferWithAvailability
from dstack._internal.core.models.profiles import RetryEvent
from dstack._internal.core.models.runs import (
    Job,
    JobStatus,
    JobTerminationReason,
    RunStatus,
)
from dstack._internal.server.db import get_session_ctx
from dstack._internal.server.models import FleetModel, InstanceModel, JobModel, RunModel
from dstack._internal.server.services.jobs import get_job_spec
from dstack._internal.server.services.runs import get_run_spec
from dstack._internal.server.services.runs.plan import get_instance_offers_from_instances
from dstack._internal.utils.common import get_current_datetime
from dstack._internal.utils.logging import get_logger

logger = get_logger(__name__)

# How many higher-priority candidate runs are fetched per placement, highest first. Every
# one that is waiting is fit-checked, in memory against instances already loaded, until
# the first fit. There is no separate cap on fit checks: any cap below this bound would let
# waiters that fit nothing hide one that fits (ADR-049 decision 2).
_PRIORITY_GATE_MAX_CANDIDATES = 50


async def find_higher_priority_waiter(
    run_model: RunModel,
    job_model: JobModel,
    job: Job,
    multinode: bool,
    fleet_model: FleetModel,
    instance_offers: list[tuple[InstanceModel, InstanceOfferWithAvailability]],
) -> Optional[str]:
    """
    Returns the name of a waiting run of strictly higher priority that fits one of
    `instance_offers`, or None when the job may take the instance. Never raises.
    """
    try:
        if not _is_gated(run_model, job_model, job, multinode, instance_offers):
            return None
        instances = [instance for instance, _ in instance_offers]
        for waiter in await _load_higher_priority_waiters(run_model):
            if _waiter_fits(waiter, fleet_model, instances):
                return waiter.run_name
        return None
    except Exception:
        logger.exception("Priority gate failed for run %s; placing without it", run_model.run_name)
        return None


def _is_gated(
    run_model: RunModel,
    job_model: JobModel,
    job: Job,
    multinode: bool,
    instance_offers: list[tuple[InstanceModel, InstanceOfferWithAvailability]],
) -> bool:
    if not instance_offers:
        return False
    # A multinode run's later jobs follow its master onto a chosen fleet; yielding them
    # would split a cluster that is already forming.
    if multinode or job_model.job_num != 0:
        return False
    return _is_within_no_capacity_window(run_model, job)


def _is_within_no_capacity_window(run_model: RunModel, job: Job) -> bool:
    """
    A yield must end the way a no-capacity wait ends. Without a retry policy that
    retries no-capacity, a yield has no bound, so the run is not gated at all.
    """
    retry = job.job_spec.retry
    if retry is None or RetryEvent.NO_CAPACITY not in retry.on_events:
        return False
    # Same anchor `_should_retry_job` uses for a run's first start.
    started_at: datetime = run_model.submitted_at
    if run_model.next_triggered_at is not None:
        started_at = run_model.next_triggered_at
    elapsed = get_current_datetime() - started_at
    return elapsed.total_seconds() < retry.duration_for(RetryEvent.NO_CAPACITY)


async def _load_higher_priority_waiters(run_model: RunModel) -> list[RunModel]:
    async with get_session_ctx() as session:
        res = await session.execute(
            select(RunModel)
            .where(
                RunModel.project_id == run_model.project_id,
                RunModel.id != run_model.id,
                RunModel.deleted == False,
                RunModel.priority > run_model.priority,
                or_(
                    RunModel.status == RunStatus.SUBMITTED,
                    and_(
                        RunModel.status == RunStatus.PENDING,
                        RunModel.resubmission_attempt > 0,
                    ),
                ),
            )
            .order_by(RunModel.priority.desc(), RunModel.submitted_at.asc())
            .limit(_PRIORITY_GATE_MAX_CANDIDATES)
            .options(selectinload(RunModel.jobs))
        )
        # Not truncated: the caller fit-checks in this order and stops at the first fit.
        return [r for r in res.scalars().all() if _latest_job_is_waiting(r) is not None]


def _latest_job_is_waiting(run_model: RunModel) -> Optional[JobModel]:
    """The run's master job, if the run is waiting for capacity rather than running."""
    master_jobs = [j for j in run_model.jobs if j.replica_num == 0 and j.job_num == 0]
    if not master_jobs:
        return None
    latest = max(master_jobs, key=lambda j: j.submission_num)
    if run_model.status == RunStatus.SUBMITTED:
        if latest.status == JobStatus.SUBMITTED and not latest.instance_assigned:
            return latest
        return None
    if latest.termination_reason == JobTerminationReason.FAILED_TO_START_DUE_TO_NO_CAPACITY:
        return latest
    return None


def _waiter_fits(
    waiter: RunModel, fleet_model: FleetModel, instances: list[InstanceModel]
) -> bool:
    latest = _latest_job_is_waiting(waiter)
    if latest is None:
        return False
    run_spec = get_run_spec(waiter)
    profile = run_spec.merged_profile
    if waiter.fleet_id is not None and waiter.fleet_id != fleet_model.id:
        return False
    if profile.fleets is not None and not any(
        ref.name == fleet_model.name
        and (ref.project is None) == (fleet_model.project_id == waiter.project_id)
        for ref in map(EntityReference.parse, profile.fleets)
    ):
        return False
    job = Job(job_spec=get_job_spec(latest), job_submissions=[])
    if job.job_spec.jobs_per_replica > 1:
        # A cluster needs several instances at once; one free slot is not a fit.
        return False
    offers = get_instance_offers_from_instances(
        instances=instances,
        run_spec=run_spec,
        job=job,
        exclude_not_available=True,
    )
    return len(offers) > 0
