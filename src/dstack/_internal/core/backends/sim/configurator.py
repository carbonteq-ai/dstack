import json
import os

from dstack._internal.core.backends.base.configurator import BackendRecord, Configurator
from dstack._internal.core.backends.sim.backend import SimBackend
from dstack._internal.core.backends.sim.models import (
    SimBackendConfig,
    SimBackendConfigWithCreds,
    SimConfig,
    SimStoredConfig,
)
from dstack._internal.core.models.backends.base import BackendType

# CARBONTEQ: the sim backend never loads by accident. `configurators.py` imports
# every configurator inside `try/except ImportError`, so raising here leaves the
# backend unregistered: a server without the variable treats `type: sim` as a
# backend it does not have, and refuses the config rather than faking a cloud.
SIM_ENABLED_ENV = "DSTACK_SIM_ENABLED"
if os.getenv(SIM_ENABLED_ENV) != "1":
    raise ImportError(f"the sim backend is disabled; set {SIM_ENABLED_ENV}=1 to enable it")


class SimConfigurator(Configurator[SimBackendConfig, SimBackendConfigWithCreds]):
    TYPE = BackendType.SIM
    BACKEND_CLASS = SimBackend

    def validate_config(self, config: SimBackendConfigWithCreds, default_creds_enabled: bool):
        # Deliberately no call to the controller: a server must boot, and a
        # project must load, while the controller is still starting.
        pass

    def create_backend(
        self, project_name: str, config: SimBackendConfigWithCreds
    ) -> BackendRecord:
        return BackendRecord(
            config=SimStoredConfig(
                **SimBackendConfig.__response__.parse_obj(config).dict()
            ).json(),
            auth="{}",
        )

    def get_backend_config_with_creds(self, record: BackendRecord) -> SimBackendConfigWithCreds:
        return SimBackendConfigWithCreds.__response__.parse_obj(self._get_config(record))

    def get_backend_config_without_creds(self, record: BackendRecord) -> SimBackendConfig:
        return SimBackendConfig.__response__.parse_obj(self._get_config(record))

    def get_backend(self, record: BackendRecord) -> SimBackend:
        return SimBackend(config=self._get_config(record))

    def _get_config(self, record: BackendRecord) -> SimConfig:
        return SimConfig.__response__(**json.loads(record.config))
