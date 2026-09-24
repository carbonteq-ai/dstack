import json
import uuid
from datetime import timedelta
from typing import Optional
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dstack._internal.core.models.configurations import ScalingSpec, ServiceConfiguration
from dstack._internal.core.models.instances import InstanceStatus
from dstack._internal.core.models.profiles import Profile, SpotPolicy
from dstack._internal.core.models.resources import Range
from dstack._internal.core.models.runs import (
    JobStatus,
    JobTerminationReason,
    RunStatus,
)
from dstack._internal.server.background.pipeline_tasks.runs import RunWorker
from dstack._internal.server.background.pipeline_tasks.runs.pending import _get_retry_delay
from dstack._internal.server.models import JobModel
from dstack._internal.server.testing.common import (
    create_instance,
    create_job,
    create_project,
    create_repo,
    create_run,
    create_user,
    get_job_provisioning_data,
    get_run_spec,
)
from dstack._internal.utils.common import get_current_datetime
from tests._internal.server.background.pipeline_tasks.test_runs.helpers import (
    lock_run,
    run_to_pipeline_item,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("test_db", ["sqlite", "postgres"], indirect=True)
@pytest.mark.usefixtures("image_config_mock")
class TestRunPendingWorker:
    async def test_submits_non_service_run_and_creates_job(
        self, test_db, session: AsyncSession, worker: RunWorker
    ) -> None:
        project = await create_project(session=session)
        user = await create_user(session=session)
        repo = await create_repo(session=session, project_id=project.id)
        run = await create_run(
            session=session,
            project=project,
            repo=repo,
            user=user,
            status=RunStatus.PENDING,
            resubmission_attempt=0,
            next_triggered_at=None,
        )
        lock_run(run)
        await session.commit()

        await worker.process(run_to_pipeline_item(run))

        await session.refresh(run)
        assert run.status == RunStatus.SUBMITTED
        assert run.desired_replica_count == 1
        assert run.lock_token is None
        assert run.lock_expires_at is None
        assert run.lock_owner is None

        res = await session.execute(select(JobModel).where(JobModel.run_id == run.id))
        jobs = list(res.scalars().all())
        assert len(jobs) == 1
        assert jobs[0].status == JobStatus.SUBMITTED
        assert jobs[0].replica_num == 0
        assert jobs[0].submission_num == 0

    async def test_skips_retrying_run_when_delay_not_met(
        self, test_db, session: AsyncSession, worker: RunWorker
    ) -> None:
        project = await create_project(session=session)
        user = await create_user(session=session)
        repo = await create_repo(session=session, project_id=project.id)
        run = await create_run(
            session=session,
            project=project,
            repo=repo,
            user=user,
            status=RunStatus.PENDING,
            resubmission_attempt=1,
        )
        # Create a job with recent last_processed_at so retry delay is not met
        await create_job(
            session=session,
            run=run,
            status=JobStatus.FAILED,
            last_processed_at=get_current_datetime(),
        )
        lock_run(run)
        await session.commit()

        await worker.process(run_to_pipeline_item(run))

        await session.refresh(run)
        assert run.status == RunStatus.PENDING
        assert run.lock_token is None
        assert run.lock_expires_at is None
        assert run.lock_owner is None

    async def test_resubmits_retrying_run_after_delay(
        self, test_db, session: AsyncSession, worker: RunWorker
    ) -> None:
        project = await create_project(session=session)
        user = await create_user(session=session)
        repo = await create_repo(session=session, project_id=project.id)
        run = await create_run(
            session=session,
            project=project,
            repo=repo,
            user=user,
            status=RunStatus.PENDING,
            resubmission_attempt=1,
        )
        # Create a job with old last_processed_at so retry delay is met (>15s for attempt 1)
        old_time = get_current_datetime() - timedelta(minutes=1)
        old_job = await create_job(
            session=session,
            run=run,
            status=JobStatus.FAILED,
            last_processed_at=old_time,
        )
        lock_run(run)
        await session.commit()

        await worker.process(run_to_pipeline_item(run))

        await session.refresh(run)
        assert run.status == RunStatus.SUBMITTED
        assert run.desired_replica_count == 1
        assert run.lock_token is None
        assert run.lock_expires_at is None
        assert run.lock_owner is None

        # Should have created a new job (retry of the failed one)
        res = await session.execute(
            select(JobModel)
            .where(JobModel.run_id == run.id)
            .order_by(JobModel.submitted_at.desc())
        )
        jobs = list(res.scalars().all())
        assert len(jobs) == 2
        new_job = next(j for j in jobs if j.id != old_job.id)
        assert new_job.status == JobStatus.SUBMITTED
        assert new_job.replica_num == old_job.replica_num
        assert new_job.submission_num == old_job.submission_num + 1

    async def test_resubmission_deletes_superseded_no_capacity_submissions(
        self, test_db, session: AsyncSession, worker: RunWorker
    ) -> None:
        """
        On resubmission, older submissions that failed to start due to no capacity
        without provisioning are deleted. The direct predecessor, provisioned
        submissions, and submissions failed for other reasons are kept.
        """
        project = await create_project(session=session)
        user = await create_user(session=session)
        repo = await create_repo(session=session, project_id=project.id)
        run = await create_run(
            session=session,
            project=project,
            repo=repo,
            user=user,
            status=RunStatus.PENDING,
            resubmission_attempt=4,
            retry_state='{"no-capacity":{"attempts":4,"first_at":"2023-01-02T03:04:00+00:00"}}',
        )
        old_time = get_current_datetime() - timedelta(minutes=10)
        superseded_job = await create_job(
            session=session,
            run=run,
            status=JobStatus.FAILED,
            last_processed_at=old_time,
            submission_num=0,
            termination_reason=JobTerminationReason.FAILED_TO_START_DUE_TO_NO_CAPACITY,
        )
        provisioned_job = await create_job(
            session=session,
            run=run,
            status=JobStatus.FAILED,
            last_processed_at=old_time,
            submission_num=1,
            termination_reason=JobTerminationReason.FAILED_TO_START_DUE_TO_NO_CAPACITY,
            job_provisioning_data=get_job_provisioning_data(),
        )
        other_reason_job = await create_job(
            session=session,
            run=run,
            status=JobStatus.FAILED,
            last_processed_at=old_time,
            submission_num=2,
            termination_reason=JobTerminationReason.TERMINATED_BY_USER,
        )
        predecessor_job = await create_job(
            session=session,
            run=run,
            status=JobStatus.FAILED,
            last_processed_at=old_time,
            submission_num=3,
            termination_reason=JobTerminationReason.FAILED_TO_START_DUE_TO_NO_CAPACITY,
        )
        lock_run(run)
        await session.commit()

        await worker.process(run_to_pipeline_item(run))

        await session.refresh(run)
        assert run.status == RunStatus.SUBMITTED
        res = await session.execute(
            select(JobModel).where(JobModel.run_id == run.id).order_by(JobModel.submission_num)
        )
        jobs = list(res.scalars().all())
        assert [j.submission_num for j in jobs] == [1, 2, 3, 4]
        assert superseded_job.id not in [j.id for j in jobs]
        assert provisioned_job.id in [j.id for j in jobs]
        assert other_reason_job.id in [j.id for j in jobs]
        assert predecessor_job.id in [j.id for j in jobs]
        assert json.loads(run.retry_state) == {
            "no-capacity": {"attempts": 4, "first_at": "2023-01-02T03:04:00+00:00"}
        }
        assert jobs[-1].status == JobStatus.SUBMITTED

    async def test_noops_when_run_lock_changes_after_processing(
        self, test_db, session: AsyncSession, worker: RunWorker
    ) -> None:
        project = await create_project(session=session)
        user = await create_user(session=session)
        repo = await create_repo(session=session, project_id=project.id)
        run = await create_run(
            session=session,
            project=project,
            repo=repo,
            user=user,
            status=RunStatus.PENDING,
            resubmission_attempt=0,
            next_triggered_at=None,
        )
        lock_run(run)
        await session.commit()
        item = run_to_pipeline_item(run)
        new_lock_token = uuid.uuid4()

        from dstack._internal.server.background.pipeline_tasks.runs.pending import (
            PendingResult,
            PendingRunUpdateMap,
        )

        async def intercept_process(context):
            # Change the lock token to simulate concurrent modification
            run.lock_token = new_lock_token
            run.lock_expires_at = get_current_datetime() + timedelta(minutes=1)
            await session.commit()
            # Return a result that would normally cause a state change
            return PendingResult(
                run_update_map=PendingRunUpdateMap(
                    status=RunStatus.SUBMITTED,
                    desired_replica_count=1,
                ),
                new_job_models=[],
            )

        with patch(
            "dstack._internal.server.background.pipeline_tasks.runs.pending.process_pending_run",
            new=AsyncMock(side_effect=intercept_process),
        ):
            await worker.process(item)

        await session.refresh(run)
        assert run.status == RunStatus.PENDING
        assert run.lock_token == new_lock_token

    async def test_submits_service_run_and_creates_jobs(
        self, test_db, session: AsyncSession, worker: RunWorker
    ) -> None:
        project = await create_project(session=session)
        user = await create_user(session=session)
        repo = await create_repo(session=session, project_id=project.id)
        run_spec = get_run_spec(
            repo_id=repo.name,
            run_name="service-run",
            configuration=ServiceConfiguration(
                port=8080,
                commands=["echo Hi!"],
                replicas=Range[int](min=2, max=2),
            ),
        )
        run = await create_run(
            session=session,
            project=project,
            repo=repo,
            user=user,
            run_name="service-run",
            run_spec=run_spec,
            status=RunStatus.PENDING,
            resubmission_attempt=0,
            next_triggered_at=None,
        )
        lock_run(run)
        await session.commit()

        await worker.process(run_to_pipeline_item(run))

        await session.refresh(run)
        assert run.status == RunStatus.SUBMITTED
        assert run.desired_replica_count == 2
        assert run.desired_replica_counts is not None
        counts = json.loads(run.desired_replica_counts)
        assert counts == {"0": 2}
        assert run.lock_token is None
        assert run.lock_expires_at is None
        assert run.lock_owner is None

        res = await session.execute(select(JobModel).where(JobModel.run_id == run.id))
        jobs = list(res.scalars().all())
        assert len(jobs) == 2
        replica_nums = sorted(j.replica_num for j in jobs)
        assert replica_nums == [0, 1]
        assert all(j.status == JobStatus.SUBMITTED for j in jobs)
        assert all(j.submission_num == 0 for j in jobs)

    async def test_noops_for_zero_scaled_service(
        self, test_db, session: AsyncSession, worker: RunWorker
    ) -> None:
        project = await create_project(session=session)
        user = await create_user(session=session)
        repo = await create_repo(session=session, project_id=project.id)
        run_spec = get_run_spec(
            repo_id=repo.name,
            run_name="service-run",
            configuration=ServiceConfiguration(
                port=8080,
                commands=["echo Hi!"],
                replicas=Range[int](min=0, max=2),
                scaling=ScalingSpec(metric="rps", target=10),
            ),
        )
        run = await create_run(
            session=session,
            project=project,
            repo=repo,
            user=user,
            run_name="service-run",
            run_spec=run_spec,
            status=RunStatus.PENDING,
            resubmission_attempt=0,
            next_triggered_at=None,
        )
        # Set desired_replica_count=0 and desired_replica_counts to match zero-scaled state.
        run.desired_replica_count = 0
        run.desired_replica_counts = json.dumps({"0": 0})
        lock_run(run)
        await session.commit()

        await worker.process(run_to_pipeline_item(run))

        await session.refresh(run)
        assert run.status == RunStatus.PENDING
        assert run.lock_token is None
        assert run.lock_expires_at is None
        assert run.lock_owner is None

        res = await session.execute(select(JobModel).where(JobModel.run_id == run.id))
        jobs = list(res.scalars().all())
        assert len(jobs) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("test_db", ["sqlite", "postgres"], indirect=True)
@pytest.mark.usefixtures("image_config_mock")
class TestWakeOnRelease:
    """
    CarbonTeq delta (D-49). A run waiting for capacity is retried as soon as an instance
    in its project frees a block after its last attempt began, not at its next backoff
    step. Measured on production 2026-09-16: slots idled 5 and 8 minutes while runs that
    fitted them sat between backoff steps.
    """

    async def _waiting_run(
        self,
        session: AsyncSession,
        *,
        attempt_submitted_ago: timedelta = timedelta(seconds=20),
        termination_reason: JobTerminationReason = (
            JobTerminationReason.FAILED_TO_START_DUE_TO_NO_CAPACITY
        ),
        spot_policy: Optional[SpotPolicy] = None,
    ):
        project = await create_project(session=session)
        user = await create_user(session=session)
        repo = await create_repo(session=session, project_id=project.id)
        # A dev environment with no spot policy requires on-demand, as the default
        # instance is, so the cases that do not name a market release into the same one.
        run_spec = get_run_spec(
            repo_id=repo.name, profile=Profile(name="default", spot_policy=spot_policy)
        )
        # Attempt 5 is the five-minute step, so only a wake can resubmit it within the test.
        run = await create_run(
            session=session,
            project=project,
            repo=repo,
            user=user,
            status=RunStatus.PENDING,
            resubmission_attempt=5,
            run_spec=run_spec,
        )
        now = get_current_datetime()
        await create_job(
            session=session,
            run=run,
            status=JobStatus.FAILED,
            submitted_at=now - attempt_submitted_ago,
            last_processed_at=now,
            termination_reason=termination_reason,
        )
        return project, run

    async def _process(self, session: AsyncSession, worker: RunWorker, run) -> None:
        lock_run(run)
        await session.commit()
        await worker.process(run_to_pipeline_item(run))
        await session.refresh(run)

    async def test_wake_on_release_resubmits_before_the_backoff_step(
        self, test_db, session: AsyncSession, worker: RunWorker
    ) -> None:
        project, run = await self._waiting_run(session)
        instance = await create_instance(session=session, project=project)
        instance.last_job_processed_at = get_current_datetime() - timedelta(seconds=5)

        await self._process(session, worker, run)

        assert run.status == RunStatus.SUBMITTED

    async def test_wake_on_release_counts_a_release_while_the_attempt_was_failing(
        self, test_db, session: AsyncSession, worker: RunWorker
    ) -> None:
        # The 20:45:04 case: the attempt found no capacity, the slot freed a second later,
        # and the attempt's job was marked failed after that. Its last_processed_at is
        # later than the release; its submitted_at is not.
        project, run = await self._waiting_run(
            session, attempt_submitted_ago=timedelta(seconds=16)
        )
        instance = await create_instance(session=session, project=project)
        instance.last_job_processed_at = get_current_datetime() - timedelta(seconds=1)

        await self._process(session, worker, run)

        assert run.status == RunStatus.SUBMITTED

    async def test_wake_on_release_ignores_a_release_before_the_attempt(
        self, test_db, session: AsyncSession, worker: RunWorker
    ) -> None:
        project, run = await self._waiting_run(session)
        instance = await create_instance(session=session, project=project)
        instance.last_job_processed_at = get_current_datetime() - timedelta(minutes=1)

        await self._process(session, worker, run)

        assert run.status == RunStatus.PENDING

    async def test_wake_on_release_ignores_an_instance_with_no_free_block(
        self, test_db, session: AsyncSession, worker: RunWorker
    ) -> None:
        project, run = await self._waiting_run(session)
        instance = await create_instance(
            session=session,
            project=project,
            status=InstanceStatus.BUSY,
            total_blocks=1,
            busy_blocks=1,
        )
        instance.last_job_processed_at = get_current_datetime() - timedelta(seconds=5)

        await self._process(session, worker, run)

        assert run.status == RunStatus.PENDING

    async def test_wake_on_release_frees_a_block_on_a_shared_instance(
        self, test_db, session: AsyncSession, worker: RunWorker
    ) -> None:
        project, run = await self._waiting_run(session)
        instance = await create_instance(
            session=session,
            project=project,
            status=InstanceStatus.BUSY,
            total_blocks=4,
            busy_blocks=3,
        )
        instance.last_job_processed_at = get_current_datetime() - timedelta(seconds=5)

        await self._process(session, worker, run)

        assert run.status == RunStatus.SUBMITTED

    async def test_wake_on_release_ignores_another_projects_instance(
        self, test_db, session: AsyncSession, worker: RunWorker
    ) -> None:
        _, run = await self._waiting_run(session)
        other_owner = await create_user(session=session, name="other_owner")
        other = await create_project(session=session, owner=other_owner, name="other")
        instance = await create_instance(session=session, project=other)
        instance.last_job_processed_at = get_current_datetime() - timedelta(seconds=5)

        await self._process(session, worker, run)

        assert run.status == RunStatus.PENDING

    async def test_wake_on_release_ignores_unreachable_and_terminating_instances(
        self, test_db, session: AsyncSession, worker: RunWorker
    ) -> None:
        project, run = await self._waiting_run(session)
        released = get_current_datetime() - timedelta(seconds=5)
        unreachable = await create_instance(
            session=session, project=project, unreachable=True, name="unreachable"
        )
        unreachable.last_job_processed_at = released
        terminating = await create_instance(
            session=session,
            project=project,
            status=InstanceStatus.TERMINATING,
            name="terminating",
        )
        terminating.last_job_processed_at = released

        await self._process(session, worker, run)

        assert run.status == RunStatus.PENDING

    async def test_wake_on_release_keeps_the_backoff_for_other_failures(
        self, test_db, session: AsyncSession, worker: RunWorker
    ) -> None:
        # An error or interruption retry is a crash loop, not a wait for a slot.
        project, run = await self._waiting_run(
            session, termination_reason=JobTerminationReason.CONTAINER_EXITED_WITH_ERROR
        )
        instance = await create_instance(session=session, project=project)
        instance.last_job_processed_at = get_current_datetime() - timedelta(seconds=5)

        await self._process(session, worker, run)

        assert run.status == RunStatus.PENDING

    # ADR-049 decision 3: the wake is scoped to the market. Placement honours the run's
    # spot requirement, so a release in the other market is capacity the waiter can never
    # use, and waking it spends an attempt (and on a cloud fleet a placeholder) for nothing.

    async def test_wake_on_release_ignores_an_on_demand_release_for_a_spot_waiter(
        self, test_db, session: AsyncSession, worker: RunWorker
    ) -> None:
        project, run = await self._waiting_run(session, spot_policy=SpotPolicy.SPOT)
        instance = await create_instance(session=session, project=project, spot=False)
        instance.last_job_processed_at = get_current_datetime() - timedelta(seconds=5)

        await self._process(session, worker, run)

        assert run.status == RunStatus.PENDING

    async def test_wake_on_release_ignores_a_spot_release_for_an_on_demand_waiter(
        self, test_db, session: AsyncSession, worker: RunWorker
    ) -> None:
        project, run = await self._waiting_run(session, spot_policy=SpotPolicy.ONDEMAND)
        instance = await create_instance(session=session, project=project, spot=True)
        instance.last_job_processed_at = get_current_datetime() - timedelta(seconds=5)

        await self._process(session, worker, run)

        assert run.status == RunStatus.PENDING

    @pytest.mark.parametrize(
        ("spot_policy", "instance_spot"),
        [
            (SpotPolicy.SPOT, True),
            (SpotPolicy.ONDEMAND, False),
            (SpotPolicy.AUTO, True),
            (SpotPolicy.AUTO, False),
        ],
    )
    async def test_wake_on_release_wakes_a_waiter_in_the_same_market(
        self,
        test_db,
        session: AsyncSession,
        worker: RunWorker,
        spot_policy: SpotPolicy,
        instance_spot: bool,
    ) -> None:
        # `auto` competes for both markets, so a release in either wakes it.
        project, run = await self._waiting_run(session, spot_policy=spot_policy)
        instance = await create_instance(session=session, project=project, spot=instance_spot)
        instance.last_job_processed_at = get_current_datetime() - timedelta(seconds=5)

        await self._process(session, worker, run)

        assert run.status == RunStatus.SUBMITTED

    async def test_wake_on_release_finds_a_same_market_release_beside_another_market(
        self, test_db, session: AsyncSession, worker: RunWorker
    ) -> None:
        # The market is read after the query, so the query must not stop at the first
        # release it finds: here the three latest releases are in the other market.
        project, run = await self._waiting_run(session, spot_policy=SpotPolicy.SPOT)
        released = get_current_datetime() - timedelta(seconds=5)
        for i in range(3):
            on_demand = await create_instance(
                session=session, project=project, spot=False, name=f"on-demand-{i}"
            )
            on_demand.last_job_processed_at = released
        spot = await create_instance(session=session, project=project, spot=True, name="spot")
        spot.last_job_processed_at = released - timedelta(seconds=1)

        await self._process(session, worker, run)

        assert run.status == RunStatus.SUBMITTED

    async def test_wake_on_release_fails_open_on_an_instance_without_an_offer(
        self, test_db, session: AsyncSession, worker: RunWorker
    ) -> None:
        # No market to read is not evidence of the wrong market: wake, as before the scoping.
        project, run = await self._waiting_run(session, spot_policy=SpotPolicy.SPOT)
        instance = await create_instance(session=session, project=project, offer=None)
        instance.last_job_processed_at = get_current_datetime() - timedelta(seconds=5)

        await self._process(session, worker, run)

        assert run.status == RunStatus.SUBMITTED


def test_retry_backoff_has_stable_twenty_percent_jitter_and_ten_minute_base_cap() -> None:
    run_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    delays = [_get_retry_delay(attempt, run_id) for attempt in range(1, 9)]

    bases = [15, 30, 60, 120, 300, 600, 600, 600]
    for delay, base in zip(delays, bases):
        assert timedelta(seconds=base * 0.8) <= delay <= timedelta(seconds=base * 1.2)
    assert delays == [_get_retry_delay(attempt, run_id) for attempt in range(1, 9)]
