from typing import Annotated, List, Literal, Optional, Union

from pydantic import Field, root_validator, validator

from dstack._internal.core.models.common import CoreModel
from dstack._internal.core.models.provisioning_preconditions import (
    HTTPBearerCredentials,
    HTTPImageReadinessConfig,
    ResolvedHTTPImageReadinessConfig,
)
from dstack._internal.core.models.resources import Memory
from dstack._internal.core.models.volumes import VolumeMountPoint

RUNPOD_COMMUNITY_CLOUD_DEFAULT = False


class RunpodRunStorageConfig(CoreModel):
    region: Annotated[
        Optional[str],
        Field(
            description=(
                "A fixed Secure Cloud data center for run volumes. Omit to select from "
                "the backend regions using current job offers"
            )
        ),
    ] = None
    size: Annotated[Memory, Field(description="The size of each run volume")]
    path: Annotated[
        str,
        Field(description="The absolute path where the run volume is mounted"),
    ] = "/workspace"

    @validator("region")
    def validate_secure_cloud_region(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        if "-" not in value:
            raise ValueError("RunPod network volumes require a Secure Cloud data center")
        return value

    @validator("size")
    def validate_size(cls, value: Memory) -> Memory:
        if value < 10:
            raise ValueError("RunPod network volumes must be at least 10GB")
        return value

    @validator("path")
    def validate_path(cls, value: str) -> str:
        return VolumeMountPoint(name="run-storage", path=value).path


class RunpodAPIKeyCreds(CoreModel):
    type: Literal["api_key"] = "api_key"
    api_key: Annotated[str, Field(description="The API key")]
    provisioning_precondition: Optional[HTTPBearerCredentials] = None


AnyRunpodCreds = RunpodAPIKeyCreds
RunpodCreds = AnyRunpodCreds


class RunpodBackendConfig(CoreModel):
    type: Literal["runpod"] = "runpod"
    regions: Annotated[
        Optional[List[str]],
        Field(description="The list of Runpod regions. Omit to use all regions"),
    ] = None
    community_cloud: Annotated[
        Optional[bool],
        Field(
            description=(
                "Whether Community Cloud offers can be suggested in addition to Secure Cloud."
                f" Defaults to `{str(RUNPOD_COMMUNITY_CLOUD_DEFAULT).lower()}`"
            )
        ),
    ] = None
    minimum_stock_status: Annotated[
        Literal["low", "medium", "high"],
        Field(
            description=(
                "Minimum RunPod live stock status for GPU spot offers. Defaults to `low` "
                "to preserve upstream behavior"
            )
        ),
    ] = "low"
    provisioning_precondition: Optional[HTTPImageReadinessConfig] = None
    provisioning_timeout_seconds: Annotated[
        Optional[int],
        Field(
            ge=600,
            le=3600,
            description=(
                "Maximum time from Pod creation until the runner is ready, including "
                "container image pull and unpack. Omit to use the server default"
            ),
        ),
    ] = None
    run_storage: Annotated[
        Optional[RunpodRunStorageConfig],
        Field(
            description=(
                "Create one managed network volume for each single-node spot task, reuse it "
                "across retries, and delete it after the run finishes"
            )
        ),
    ] = None

    @root_validator
    def validate_run_storage_regions(cls, values):
        run_storage = values.get("run_storage")
        regions = values.get("regions")
        if run_storage is None or run_storage.region is not None:
            return values
        if not regions:
            raise ValueError(
                "RunPod managed run storage requires backend regions or a fixed run_storage region"
            )
        if any("-" not in region for region in regions):
            raise ValueError("RunPod network volumes require Secure Cloud data centers")
        return values


class RunpodBackendConfigWithCreds(RunpodBackendConfig):
    creds: Annotated[AnyRunpodCreds, Field(description="The credentials")]


AnyRunpodBackendConfig = Union[RunpodBackendConfig, RunpodBackendConfigWithCreds]


class RunpodStoredConfig(RunpodBackendConfig):
    pass


class RunpodConfig(RunpodStoredConfig):
    creds: AnyRunpodCreds

    @property
    def allow_community_cloud(self) -> bool:
        if self.community_cloud is not None:
            return self.community_cloud
        return RUNPOD_COMMUNITY_CLOUD_DEFAULT

    def resolved_provisioning_precondition(
        self,
    ) -> Optional[ResolvedHTTPImageReadinessConfig]:
        if self.provisioning_precondition is None:
            return None
        credentials = self.creds.provisioning_precondition
        if credentials is None:
            return None
        return ResolvedHTTPImageReadinessConfig(
            **self.provisioning_precondition.dict(),
            credentials=credentials,
        )
