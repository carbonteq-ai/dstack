from dstack._internal.core.backends.base.backend import Backend
from dstack._internal.core.backends.runpod.compute import RunpodCompute
from dstack._internal.core.backends.runpod.models import RunpodConfig
from dstack._internal.core.models.backends.base import BackendType
from dstack._internal.core.models.provisioning_preconditions import (
    ResolvedHTTPImageReadinessConfig,
)


class RunpodBackend(Backend):
    TYPE = BackendType.RUNPOD
    COMPUTE_CLASS = RunpodCompute

    def __init__(self, config: RunpodConfig):
        self.config = config
        self._compute = RunpodCompute(self.config)

    def compute(self) -> RunpodCompute:
        return self._compute

    def get_image_readiness_precondition(
        self,
    ) -> ResolvedHTTPImageReadinessConfig | None:
        return self.config.resolved_provisioning_precondition()
