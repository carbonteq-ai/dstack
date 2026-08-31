"""Builders for the dstack models the usage code reads.

Real `Run` objects rather than stand-ins on purpose. The accounting reaches into
`run.jobs[].job_submissions[]`, `job_provisioning_data.backend` and
`run_spec.merged_profile`, so a duck-typed fake would keep passing if any of
those moved — which is exactly the breakage a rebase would cause.
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from dstack._internal.core.models.backends.base import BackendType
from dstack._internal.core.models.configurations import TaskConfiguration
from dstack._internal.core.models.instances import InstanceType, Resources
from dstack._internal.core.models.profiles import Profile
from dstack._internal.core.models.resources import ResourcesSpec
from dstack._internal.core.models.runs import (
    Job,
    JobProvisioningData,
    JobSpec,
    JobStatus,
    JobSubmission,
    Requirements,
    Run,
    RunSpec,
    RunStatus,
)

UTC = timezone.utc


def provisioning_data(backend: BackendType = BackendType.REMOTE, price: float = 0.0):
    return JobProvisioningData(
        backend=backend,
        instance_type=InstanceType(
            name="test-instance",
            resources=Resources(cpus=2, memory_mib=8192, gpus=[], spot=False),
        ),
        instance_id="i-test",
        region="onprem",
        price=price,
        username="dstack",
        ssh_port=22,
        dockerized=True,
        ssh_proxy=None,
    )


def submission(
    *,
    submitted_at: datetime,
    finished_at: Optional[datetime] = None,
    status: JobStatus = JobStatus.RUNNING,
    backend: Optional[BackendType] = BackendType.REMOTE,
    price: float = 0.0,
) -> JobSubmission:
    return JobSubmission(
        id=uuid.uuid4(),
        submission_num=0,
        submitted_at=submitted_at,
        last_processed_at=finished_at or submitted_at,
        finished_at=finished_at,
        status=status,
        job_provisioning_data=(provisioning_data(backend, price) if backend is not None else None),
    )


def run(
    *,
    project: str = "team-research",
    user: str = "alice",
    submitted_at: Optional[datetime] = None,
    submissions: Optional[List[JobSubmission]] = None,
    max_duration=None,
    max_price: Optional[float] = None,
    backends: Optional[List[BackendType]] = None,
    status: RunStatus = RunStatus.RUNNING,
    cost: float = 0.0,
    run_name: str = "test-run",
) -> Run:
    submitted_at = submitted_at or datetime(2026, 8, 10, tzinfo=UTC)
    submissions = submissions or [submission(submitted_at=submitted_at)]
    spec = RunSpec(
        run_name=run_name,
        configuration=TaskConfiguration(
            image="ubuntu",
            max_duration=max_duration,
            max_price=max_price,
            backends=backends,
        ),
        profile=Profile(name="default"),
        ssh_key_pub="ssh-rsa AAAA",
    )
    built = Run(
        id=uuid.uuid4(),
        project_name=project,
        user=user,
        submitted_at=submitted_at,
        last_processed_at=submitted_at,
        status=status,
        run_spec=spec,
        jobs=[Job(job_spec=_job_spec(run_name), job_submissions=submissions)],
    )
    # `cost` is computed server-side from price x duration; tests set it directly
    # so they can pin a number without reimplementing dstack's arithmetic.
    built.cost = cost
    return built


def _job_spec(run_name: str) -> JobSpec:
    return JobSpec(
        replica_num=0,
        job_num=0,
        job_name=f"{run_name}-0-0",
        jobs_per_replica=1,
        app_specs=[],
        commands=["echo hi"],
        env={},
        home_dir="/root",
        image_name="ubuntu",
        max_duration=None,
        registry_auth=None,
        requirements=Requirements(resources=ResourcesSpec(), max_price=None, spot=None),
        retry=None,
        working_dir=".",
    )


def hours(n: float) -> timedelta:
    return timedelta(hours=n)
