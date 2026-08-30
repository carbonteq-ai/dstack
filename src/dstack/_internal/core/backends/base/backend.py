from abc import ABC, abstractmethod
from typing import ClassVar, Optional

from dstack._internal.core.backends.base.compute import Compute
from dstack._internal.core.models.backends.base import BackendType
from dstack._internal.core.models.provisioning_preconditions import (
    ResolvedHTTPImageReadinessConfig,
)


class Backend(ABC):
    TYPE: ClassVar[BackendType]
    # `COMPUTE_CLASS` is used to introspect compute features without initializing it.
    COMPUTE_CLASS: ClassVar[type[Compute]]

    @abstractmethod
    def compute(self) -> Compute:
        """
        Returns Compute instance.
        """
        pass

    def get_image_readiness_precondition(
        self,
    ) -> Optional[ResolvedHTTPImageReadinessConfig]:
        """Return the backend's pre-create image guard, if configured."""

        return None
