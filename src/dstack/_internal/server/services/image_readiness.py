from dataclasses import dataclass
from datetime import timedelta
from typing import Optional
from urllib.parse import quote, urljoin

import httpx

from dstack._internal.core.backends.base.backend import Backend
from dstack._internal.core.models.provisioning_preconditions import (
    HTTPImageReadinessConfig,
    ImageReadinessSnapshot,
    ImageReadinessState,
    ResolvedHTTPImageReadinessConfig,
)
from dstack._internal.utils.common import get_current_datetime
from dstack._internal.utils.docker import parse_image_name


@dataclass(frozen=True)
class ImageReadinessResult:
    snapshot: ImageReadinessSnapshot
    message: Optional[str] = None

    @property
    def is_ready(self) -> bool:
        return self.snapshot.state == ImageReadinessState.READY

    @property
    def is_terminal(self) -> bool:
        return self.snapshot.state in {
            ImageReadinessState.TIMED_OUT,
            ImageReadinessState.FAILED,
        }


async def evaluate_image_readiness(
    backend: Backend,
    image_name: str,
    persisted_snapshot: Optional[str],
) -> Optional[ImageReadinessResult]:
    config = backend.get_image_readiness_precondition()
    if config is not None and not isinstance(config, ResolvedHTTPImageReadinessConfig):
        config = None
    if config is None:
        if persisted_snapshot is None:
            return None
        return _failed_snapshot(
            backend=backend,
            image_name=image_name,
            persisted_snapshot=persisted_snapshot,
            safe_code="configuration-removed",
            message="Image readiness configuration changed while the job was waiting",
        )

    parsed = parse_image_name(image_name)
    if parsed.registry is None or parsed.digest is None or not _is_sha256_digest(parsed.digest):
        return _new_failed_snapshot(
            backend=backend,
            image_name=image_name,
            config=config,
            safe_code="immutable-image-required",
            message="The selected backend requires an explicit registry and sha256 image digest",
        )

    now = get_current_datetime()
    snapshot = _load_or_create_snapshot(
        backend=backend,
        image_name=image_name,
        repository=parsed.repo,
        digest=parsed.digest,
        config=config,
        persisted_snapshot=persisted_snapshot,
        now=now,
    )
    if isinstance(snapshot, ImageReadinessResult):
        return snapshot
    if now >= snapshot.deadline:
        snapshot.state = ImageReadinessState.TIMED_OUT
        snapshot.updated_at = now
        snapshot.safe_code = "readiness-timeout"
        return ImageReadinessResult(
            snapshot=snapshot,
            message="Image readiness timed out before provider provisioning",
        )

    state, safe_code = await _probe(config=config, snapshot=snapshot)
    snapshot.state = state
    snapshot.updated_at = now
    snapshot.safe_code = safe_code
    message = None
    if state == ImageReadinessState.FAILED:
        message = f"Image readiness failed before provider provisioning ({safe_code})"
    return ImageReadinessResult(snapshot=snapshot, message=message)


async def _probe(
    config: ResolvedHTTPImageReadinessConfig,
    snapshot: ImageReadinessSnapshot,
) -> tuple[ImageReadinessState, Optional[str]]:
    url = urljoin(
        str(config.url).rstrip("/") + "/",
        f"{quote(snapshot.repository, safe='')}/{quote(snapshot.digest, safe='')}",
    )
    try:
        async with httpx.AsyncClient(timeout=config.request_timeout_seconds) as client:
            response = await client.get(
                url,
                headers={"Authorization": f"Bearer {config.credentials.bearer_token}"},
            )
    except httpx.HTTPError:
        return ImageReadinessState.WAITING, "request-unavailable"
    if response.status_code == 404:
        return ImageReadinessState.WAITING, "not-found"
    if response.status_code != 200:
        if response.status_code in {401, 403}:
            return ImageReadinessState.FAILED, "authorization-rejected"
        return ImageReadinessState.WAITING, f"http-{response.status_code}"
    try:
        document = response.json()
    except ValueError:
        return ImageReadinessState.FAILED, "invalid-json"
    if not isinstance(document, dict) or not isinstance(document.get("state"), str):
        return ImageReadinessState.FAILED, "invalid-response"
    if document["state"] == config.ready_state:
        return ImageReadinessState.READY, None
    return ImageReadinessState.WAITING, f"state-{document['state']}"


def _load_or_create_snapshot(
    *,
    backend: Backend,
    image_name: str,
    repository: str,
    digest: str,
    config: ResolvedHTTPImageReadinessConfig,
    persisted_snapshot: Optional[str],
    now,
) -> ImageReadinessSnapshot | ImageReadinessResult:
    public_config = HTTPImageReadinessConfig(**config.dict(exclude={"credentials"}))
    if persisted_snapshot is None:
        return ImageReadinessSnapshot(
            backend=backend.TYPE,
            image_name=image_name,
            repository=repository,
            digest=digest,
            config=public_config,
            state=ImageReadinessState.WAITING,
            started_at=now,
            updated_at=now,
            deadline=now + timedelta(seconds=config.timeout_seconds),
        )
    try:
        snapshot = ImageReadinessSnapshot.parse_raw(persisted_snapshot)
    except ValueError:
        return _new_failed_snapshot(
            backend=backend,
            image_name=image_name,
            config=config,
            safe_code="invalid-snapshot",
            message="Persisted image readiness state is invalid",
        )
    if (
        snapshot.backend != backend.TYPE
        or snapshot.image_name != image_name
        or snapshot.repository != repository
        or snapshot.digest != digest
        or snapshot.config != public_config
    ):
        snapshot.state = ImageReadinessState.FAILED
        snapshot.updated_at = now
        snapshot.safe_code = "snapshot-mismatch"
        return ImageReadinessResult(
            snapshot=snapshot,
            message="Image readiness identity or configuration changed while waiting",
        )
    return snapshot


def _new_failed_snapshot(
    *,
    backend: Backend,
    image_name: str,
    config: ResolvedHTTPImageReadinessConfig,
    safe_code: str,
    message: str,
) -> ImageReadinessResult:
    now = get_current_datetime()
    parsed = parse_image_name(image_name)
    return ImageReadinessResult(
        snapshot=ImageReadinessSnapshot(
            backend=backend.TYPE,
            image_name=image_name,
            repository=parsed.repo,
            digest=parsed.digest or "sha256:" + "0" * 64,
            config=HTTPImageReadinessConfig(**config.dict(exclude={"credentials"})),
            state=ImageReadinessState.FAILED,
            started_at=now,
            updated_at=now,
            deadline=now,
            safe_code=safe_code,
        ),
        message=message,
    )


def _failed_snapshot(
    persisted_snapshot: str,
    *,
    backend: Backend,
    image_name: str,
    safe_code: str,
    message: str,
) -> ImageReadinessResult:
    try:
        snapshot = ImageReadinessSnapshot.parse_raw(persisted_snapshot)
    except ValueError:
        now = get_current_datetime()
        parsed = parse_image_name(image_name)
        return ImageReadinessResult(
            snapshot=ImageReadinessSnapshot(
                backend=backend.TYPE,
                image_name=image_name,
                repository=parsed.repo,
                digest=parsed.digest or "sha256:" + "0" * 64,
                config=None,
                state=ImageReadinessState.FAILED,
                started_at=now,
                updated_at=now,
                deadline=now,
                safe_code="invalid-snapshot",
            ),
            message="Persisted image readiness state is invalid",
        )
    snapshot.state = ImageReadinessState.FAILED
    snapshot.updated_at = get_current_datetime()
    snapshot.safe_code = safe_code
    return ImageReadinessResult(snapshot=snapshot, message=message)


def _is_sha256_digest(value: str) -> bool:
    if not value.startswith("sha256:") or len(value) != 71:
        return False
    return all(character in "0123456789abcdef" for character in value[7:])
