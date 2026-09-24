import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from dstack._internal.core.models.configurations import ServiceConfiguration
from dstack._internal.core.models.instances import InstanceStatus
from dstack._internal.core.models.runs import JobTerminationReason, RunSpec, RunStatus
from dstack._internal.proxy.gateway.schemas.stats import PerWindowStats
from dstack._internal.server.background.pipeline_tasks.base import ItemUpdateMap
from dstack._internal.server.background.pipeline_tasks.runs.common import (
    build_scale_up_job_models,
    compute_desired_replica_counts,
)
from dstack._internal.server.models import InstanceModel, JobModel, RunModel
from dstack._internal.server.services.instances import get_instance_offer
from dstack._internal.server.services.jobs import get_job_spec
from dstack._internal.utils.common import get_current_datetime
from dstack._internal.utils.logging import get_logger

logger = get_logger(__name__)


class PendingRunUpdateMap(ItemUpdateMap, total=False):
    status: RunStatus
    desired_replica_count: int
    desired_replica_counts: Optional[str]


@dataclass
class PendingContext:
    run_model: RunModel
    run_spec: RunSpec
    secrets: dict
    locked_job_ids: set[uuid.UUID]
    gateway_stats: Optional[PerWindowStats] = None
    capacity_released: bool = False
    """CarbonTeq: an instance in the run's project freed a block after the run's last
    no-capacity attempt began. See `capacity_released_since_last_attempt()`."""


@dataclass
class PendingResult:
    run_update_map: PendingRunUpdateMap
    new_job_models: list[JobModel]


async def process_pending_run(context: PendingContext) -> Optional[PendingResult]:
    """
    Returns None if the run is not ready for processing (retry delay not met,
    zero-scaled service, etc.). Otherwise returns a result describing the
    desired state change and pre-built job models.
    """
    run_model = context.run_model
    run_spec = context.run_spec

    if (
        run_model.resubmission_attempt > 0
        and not _is_ready_for_resubmission(run_model)
        and not context.capacity_released
    ):
        return None

    if run_spec.configuration.type == "service":
        return await _process_pending_service(context)

    desired_replica_count = 1
    new_job_models = await build_scale_up_job_models(
        run_model=run_model,
        run_spec=run_spec,
        secrets=context.secrets,
        replicas_diff=desired_replica_count,
    )
    return PendingResult(
        run_update_map=PendingRunUpdateMap(
            status=RunStatus.SUBMITTED,
            desired_replica_count=desired_replica_count,
        ),
        new_job_models=new_job_models,
    )


async def _process_pending_service(context: PendingContext) -> Optional[PendingResult]:
    run_model = context.run_model
    run_spec = context.run_spec
    assert isinstance(run_spec.configuration, ServiceConfiguration)
    configuration = run_spec.configuration

    total, per_group_desired = compute_desired_replica_counts(
        run_model=run_model,
        configuration=configuration,
        gateway_stats=context.gateway_stats,
        last_scaled_at=None,
    )
    if total == 0:
        return None

    all_new_job_models: list[JobModel] = []
    next_replica_num = max((j.replica_num for j in run_model.jobs), default=-1) + 1
    for group in configuration.replica_groups:
        assert group.name is not None
        group_desired = per_group_desired.get(group.name, 0)
        if group_desired <= 0:
            continue
        new_job_models = await build_scale_up_job_models(
            run_model=run_model,
            run_spec=run_spec,
            secrets=context.secrets,
            replicas_diff=group_desired,
            group_name=group.name,
            replica_num_start=next_replica_num,
        )
        next_replica_num += group_desired
        all_new_job_models.extend(new_job_models)

    return PendingResult(
        run_update_map=PendingRunUpdateMap(
            status=RunStatus.SUBMITTED,
            desired_replica_count=total,
            desired_replica_counts=json.dumps(per_group_desired),
        ),
        new_job_models=all_new_job_models,
    )


def _is_ready_for_resubmission(run_model: RunModel) -> bool:
    if not run_model.jobs:
        # No jobs yet — should not be possible for resubmission, but allow processing.
        return True
    last_processed_at = max(job.last_processed_at for job in run_model.jobs)
    duration_since_processing = get_current_datetime() - last_processed_at
    return duration_since_processing >= _get_retry_delay(
        run_model.resubmission_attempt, run_model.id
    )


async def capacity_released_since_last_attempt(session: AsyncSession, run_model: RunModel) -> bool:
    """
    CarbonTeq delta (D-49): wake a run waiting for capacity when capacity comes back.

    A run that failed for want of capacity is otherwise retried on the backoff ladder
    below, which nothing shortens. A slot that frees just after a waiter's attempt then
    sits idle until the next step, up to ten minutes, while the waiter could use it.

    Returns True when the run's latest attempt ended `FAILED_TO_START_DUE_TO_NO_CAPACITY`
    and an instance in the run's project has released a job since that attempt was
    submitted, and still has a free block. `InstanceModel.last_job_processed_at` is set
    only when a job is unassigned (`jobs_terminating.py`), so no new state is needed.

    The anchor is the attempt's `submitted_at`, not its `last_processed_at`: an attempt
    can find no capacity, the slot can free a second later, and the attempt's job is only
    marked failed after that. Anchoring on the failure would miss that release.

    Scoped to the market (ADR-049 decision 3): the freed instance must match the waiter's
    spot requirement, the same test `requirements_to_query_filter` applies to offers
    (`q.spot = req.spot`). A spot waiter is woken only by a spot instance, an on-demand
    waiter only by an on-demand one, and an `auto` waiter by either. Placement honours that
    requirement, so a release in the other market is capacity the waiter can never use;
    waking it anyway spent an attempt, and on a cloud fleet a placeholder, per waiter per
    release. An instance whose offer cannot be read counts as a release, as it did before
    the scoping: this delta fails open.

    Otherwise deliberately loose: whether the run fits the freed instance is not checked
    here, so a wake can be wasted. That costs one attempt per waiter per release, since the
    woken attempt's own `submitted_at` postdates the release. Placement decides the fit.
    """
    if run_model.resubmission_attempt <= 0 or not run_model.jobs:
        return False
    waiting_jobs = [
        job
        for job in run_model.jobs
        if job.termination_reason == JobTerminationReason.FAILED_TO_START_DUE_TO_NO_CAPACITY
    ]
    if not waiting_jobs:
        return False
    markets = {_spot_requirement(job) for job in waiting_jobs}
    last_attempt_at = max(job.submitted_at for job in run_model.jobs)
    res = await session.execute(
        select(InstanceModel)
        .options(load_only(InstanceModel.id, InstanceModel.offer))
        .where(
            InstanceModel.project_id == run_model.project_id,
            InstanceModel.deleted == False,
            InstanceModel.unreachable == False,
            InstanceModel.status.in_([InstanceStatus.IDLE, InstanceStatus.BUSY]),
            InstanceModel.last_job_processed_at > last_attempt_at,
            or_(
                InstanceModel.busy_blocks == 0,
                and_(
                    InstanceModel.total_blocks.is_not(None),
                    InstanceModel.busy_blocks < InstanceModel.total_blocks,
                ),
            ),
        )
        .order_by(InstanceModel.last_job_processed_at.desc())
        # Any release wakes an `auto` waiter, so one row answers; otherwise the market is
        # read from each offer in memory, over a bounded number of the latest releases.
        .limit(1 if None in markets else _WAKE_CANDIDATE_LIMIT)
    )
    return any(_released_into(instance, markets) for instance in res.scalars().all())


# CarbonTeq (ADR-049 decision 3): how many of the latest releases a waiter with a market
# requirement inspects. A project with more releases than this since one attempt began,
# all in the other market, delays the wake to the backoff step, as before D-49.
_WAKE_CANDIDATE_LIMIT = 50


def _spot_requirement(job_model: JobModel) -> Optional[bool]:
    """The job's `requirements.spot`: True spot, False on-demand, None either (`auto`).
    A spec that cannot be read is treated as `auto`, so it never silences a wake."""
    try:
        return get_job_spec(job_model).requirements.spot
    except Exception:
        logger.warning("Cannot read the spot requirement of job %s", job_model.id)
        return None


def _released_into(instance_model: InstanceModel, markets: set[Optional[bool]]) -> bool:
    """Whether a release on this instance is capacity for a waiter requiring `markets`."""
    if None in markets:
        return True
    try:
        offer = get_instance_offer(instance_model)
    except Exception:
        logger.warning("Cannot read the offer of instance %s", instance_model.id)
        return True
    if offer is None:
        return True
    return offer.instance.resources.spot in markets


# We use exponentially increasing retry delays for pending runs.
# This prevents creation of too many job submissions for runs stuck in pending,
# e.g. when users set retry for a long period without capacity.
_PENDING_RETRY_DELAYS = [
    timedelta(seconds=15),
    timedelta(seconds=30),
    timedelta(minutes=1),
    timedelta(minutes=2),
    timedelta(minutes=5),
    timedelta(minutes=10),
]


def _get_retry_delay(resubmission_attempt: int, run_id: uuid.UUID) -> timedelta:
    if resubmission_attempt - 1 < len(_PENDING_RETRY_DELAYS):
        base_delay = _PENDING_RETRY_DELAYS[resubmission_attempt - 1]
    else:
        base_delay = _PENDING_RETRY_DELAYS[-1]
    # Stable per run and attempt so repeated pipeline polling does not move the
    # deadline. The base schedule remains capped at ten minutes.
    digest = hashlib.sha256(f"{run_id}:{resubmission_attempt}".encode()).digest()
    unit_interval = int.from_bytes(digest[:8], "big") / (2**64 - 1)
    jitter_factor = 0.8 + 0.4 * unit_interval
    return base_delay * jitter_factor
