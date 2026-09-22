from dstack._internal.core.backends.base.backend import Backend
from dstack._internal.core.backends.sim.compute import SimCompute
from dstack._internal.core.backends.sim.models import SimConfig
from dstack._internal.core.models.backends.base import BackendType


class SimBackend(Backend):
    TYPE = BackendType.SIM
    COMPUTE_CLASS = SimCompute

    def __init__(self, config: SimConfig):
        self.config = config
        self._compute = SimCompute(self.config)

    def compute(self) -> SimCompute:
        return self._compute
