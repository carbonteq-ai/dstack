import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

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

    Deliberately loose: whether the run fits the freed instance is not checked here, so a
    wake can be wasted. That costs one attempt per waiter per release, since the woken
    attempt's own `submitted_at` postdates the release. Placement decides the fit.
    """
    if run_model.resubmission_attempt <= 0 or not run_model.jobs:
        return False
    if not any(
        job.termination_reason == JobTerminationReason.FAILED_TO_START_DUE_TO_NO_CAPACITY
        for job in run_model.jobs
    ):
        return False
    last_attempt_at = max(job.submitted_at for job in run_model.jobs)
    res = await session.execute(
        select(InstanceModel.id)
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
        .limit(1)
    )
    return res.scalar_one_or_none() is not None


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
