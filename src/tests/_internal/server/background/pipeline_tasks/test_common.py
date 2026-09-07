from datetime import UTC, datetime, timedelta

from dstack._internal.core.models.backends.base import BackendType
from dstack._internal.server.background.pipeline_tasks.common import (
    get_provisioning_age,
    get_provisioning_timeout,
)


def test_runpod_provisioning_timeout_keeps_default_when_unconfigured():
    assert get_provisioning_timeout(BackendType.RUNPOD, "A100") == timedelta(minutes=20)


def test_provisioning_timeout_uses_persisted_provider_override():
    assert get_provisioning_timeout(
        BackendType.RUNPOD,
        "A100",
        configured_seconds=1800,
    ) == timedelta(minutes=30)


def test_provisioning_age_excludes_pre_create_readiness_wait():
    submitted_at = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)
    provisioning_started_at = datetime(2026, 8, 30, 10, 15, tzinfo=UTC)
    now = datetime(2026, 8, 30, 10, 20, tzinfo=UTC)

    assert get_provisioning_age(submitted_at, provisioning_started_at, now) == timedelta(minutes=5)


def test_provisioning_age_keeps_legacy_submission_fallback():
    submitted_at = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)
    now = datetime(2026, 8, 30, 10, 20, tzinfo=UTC)

    assert get_provisioning_age(submitted_at, None, now) == timedelta(minutes=20)
