import enum
from datetime import datetime
from typing import Annotated, Literal, Optional

from pydantic import AnyHttpUrl, Field

from dstack._internal.core.models.backends.base import BackendType
from dstack._internal.core.models.common import CoreModel


class HTTPImageReadinessConfig(CoreModel):
    type: Literal["http_image_readiness"] = "http_image_readiness"
    url: AnyHttpUrl
    timeout_seconds: Annotated[int, Field(ge=1, le=3600)] = 900
    request_timeout_seconds: Annotated[int, Field(ge=1, le=60)] = 5
    ready_state: Annotated[str, Field(min_length=1, max_length=100)] = "verified"


class HTTPBearerCredentials(CoreModel):
    bearer_token: Annotated[str, Field(min_length=1)]


class ResolvedHTTPImageReadinessConfig(HTTPImageReadinessConfig):
    credentials: HTTPBearerCredentials


class ImageReadinessState(str, enum.Enum):
    WAITING = "waiting"
    READY = "ready"
    TIMED_OUT = "timed_out"
    FAILED = "failed"


class ImageReadinessSnapshot(CoreModel):
    backend: BackendType
    image_name: str
    repository: str
    digest: str
    config: Optional[HTTPImageReadinessConfig]
    state: ImageReadinessState
    started_at: datetime
    updated_at: datetime
    deadline: datetime
    safe_code: Optional[str] = None
