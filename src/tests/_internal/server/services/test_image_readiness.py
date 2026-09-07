from datetime import timedelta
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from dstack._internal.core.models.backends.base import BackendType
from dstack._internal.core.models.provisioning_preconditions import (
    HTTPBearerCredentials,
    ImageReadinessState,
    ResolvedHTTPImageReadinessConfig,
)
from dstack._internal.server.services.image_readiness import evaluate_image_readiness
from dstack._internal.utils.common import get_current_datetime

DIGEST = "sha256:" + "a" * 64
IMAGE = f"registry.example/team/job@{DIGEST}"


def _backend(config: ResolvedHTTPImageReadinessConfig | None) -> Mock:
    backend = Mock()
    backend.TYPE = BackendType.RUNPOD
    backend.get_image_readiness_precondition.return_value = config
    return backend


def _config() -> ResolvedHTTPImageReadinessConfig:
    return ResolvedHTTPImageReadinessConfig(
        url="http://controller.local/v1/publications",
        timeout_seconds=60,
        request_timeout_seconds=2,
        credentials=HTTPBearerCredentials(bearer_token="secret"),
    )


def _response(status_code: int, document: object | None = None) -> httpx.Response:
    request = httpx.Request("GET", "http://controller.local")
    if document is None:
        return httpx.Response(status_code, request=request)
    return httpx.Response(status_code, request=request, json=document)


@pytest.mark.asyncio
async def test_absent_config_is_compatible() -> None:
    assert await evaluate_image_readiness(_backend(None), "debian", None) is None


@pytest.mark.asyncio
async def test_requires_immutable_digest_before_request() -> None:
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as get:
        result = await evaluate_image_readiness(_backend(_config()), "debian:stable", None)

    assert result is not None
    assert result.snapshot.state == ImageReadinessState.FAILED
    assert result.snapshot.safe_code == "immutable-image-required"
    get.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_then_verified_reuses_snapshot() -> None:
    with patch(
        "httpx.AsyncClient.get",
        new_callable=AsyncMock,
        side_effect=[
            _response(200, {"state": "copying"}),
            _response(200, {"state": "verified"}),
        ],
    ) as get:
        waiting = await evaluate_image_readiness(_backend(_config()), IMAGE, None)
        assert waiting is not None
        ready = await evaluate_image_readiness(_backend(_config()), IMAGE, waiting.snapshot.json())

    assert waiting.snapshot.state == ImageReadinessState.WAITING
    assert waiting.snapshot.safe_code == "state-copying"
    assert ready is not None and ready.is_ready
    assert ready.snapshot.started_at == waiting.snapshot.started_at
    assert ready.snapshot.deadline == waiting.snapshot.deadline
    request_url = str(get.await_args_list[0].args[0])
    assert request_url.endswith(f"/team%2Fjob/sha256%3A{'a' * 64}")
    assert "secret" not in waiting.snapshot.json()


@pytest.mark.asyncio
async def test_missing_receipt_waits_and_auth_failure_fails() -> None:
    with patch(
        "httpx.AsyncClient.get",
        new_callable=AsyncMock,
        side_effect=[_response(404), _response(401)],
    ):
        waiting = await evaluate_image_readiness(_backend(_config()), IMAGE, None)
        failed = await evaluate_image_readiness(_backend(_config()), IMAGE, None)

    assert waiting is not None and not waiting.is_terminal
    assert waiting.snapshot.safe_code == "not-found"
    assert failed is not None and failed.is_terminal
    assert failed.snapshot.safe_code == "authorization-rejected"


@pytest.mark.asyncio
async def test_expired_snapshot_times_out_without_request() -> None:
    now = get_current_datetime()
    config = _config()
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as get:
        initial = await evaluate_image_readiness(_backend(config), IMAGE, None)
        assert initial is not None
        initial.snapshot.started_at = now - timedelta(minutes=2)
        initial.snapshot.deadline = now - timedelta(minutes=1)
        result = await evaluate_image_readiness(_backend(config), IMAGE, initial.snapshot.json())

    assert result is not None and result.is_terminal
    assert result.snapshot.state == ImageReadinessState.TIMED_OUT
    assert result.snapshot.safe_code == "readiness-timeout"
    assert get.await_count == 1


@pytest.mark.asyncio
async def test_config_change_fails_closed() -> None:
    with patch(
        "httpx.AsyncClient.get",
        new_callable=AsyncMock,
        return_value=_response(404),
    ):
        initial = await evaluate_image_readiness(_backend(_config()), IMAGE, None)
    assert initial is not None

    changed = _config()
    changed.timeout_seconds = 120
    result = await evaluate_image_readiness(_backend(changed), IMAGE, initial.snapshot.json())

    assert result is not None and result.is_terminal
    assert result.snapshot.safe_code == "snapshot-mismatch"
