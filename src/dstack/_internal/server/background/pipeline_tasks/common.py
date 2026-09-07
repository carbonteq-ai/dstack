from datetime import datetime, timedelta

from dstack._internal.core.models.backends.base import BackendType


def get_provisioning_timeout(
    backend_type: BackendType,
    instance_type_name: str,
    configured_seconds: int | None = None,
) -> timedelta:
    """
    This timeout refers to the max time between requesting instance creation and the instance becoming ready to accept jobs.
    For container-based backends, this also includes the image pulling time.
    """
    if configured_seconds is not None:
        return timedelta(seconds=configured_seconds)
    if backend_type == BackendType.LAMBDA:
        return timedelta(minutes=30)
    if backend_type == BackendType.RUNPOD:
        return timedelta(minutes=20)
    if backend_type == BackendType.KUBERNETES:
        return timedelta(minutes=20)
    if backend_type == BackendType.SLURM:
        return timedelta(minutes=20)
    if backend_type == BackendType.OCI and instance_type_name.startswith("BM."):
        return timedelta(minutes=20)
    if backend_type == BackendType.VULTR and instance_type_name.startswith("vbm"):
        return timedelta(minutes=55)
    if backend_type == BackendType.GCP and instance_type_name == "a4-highgpu-8g":
        return timedelta(minutes=16)
    return timedelta(minutes=10)


def get_provisioning_age(
    submitted_at: datetime,
    provisioning_started_at: datetime | None,
    now: datetime,
) -> timedelta:
    return now - (provisioning_started_at or submitted_at)
