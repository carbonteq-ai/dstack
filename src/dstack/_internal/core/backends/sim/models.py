from typing import Annotated, Literal, Optional, Union

from pydantic import Field

from dstack._internal.core.models.common import CoreModel

# CARBONTEQ: the `sim` backend (ADR-048 in the control-plane repository).
#
# A backend with no cloud behind it. Its offers, stock and faults are answered
# by an HTTP controller that lives outside this tree (the control plane's
# `sim/controller/`), and its hosts are containers running a real sshd in front
# of a fake runner. The models are pure data and always importable, so a
# config that names `sim` parses everywhere; the configurator is what refuses
# to load unless `DSTACK_SIM_ENABLED=1` — see `configurator.py`.


class SimBackendConfig(CoreModel):
    type: Annotated[Literal["sim"], Field(description="The type of backend")] = "sim"
    controller_url: Annotated[
        str,
        Field(description="The base URL of the sim controller, e.g. `http://sim-controller:7070`"),
    ]
    provisioning_timeout_seconds: Annotated[
        Optional[int],
        Field(
            ge=10,
            le=3600,
            description=(
                "Maximum time from host creation until the runner is ready."
                " Omit to use the server default"
            ),
        ),
    ] = None


class SimBackendConfigWithCreds(SimBackendConfig):
    """The sim backend has no credentials; the controller sits on a private network."""

    pass


AnySimBackendConfig = Union[SimBackendConfig, SimBackendConfigWithCreds]


class SimStoredConfig(SimBackendConfig):
    pass


class SimConfig(SimStoredConfig):
    pass
