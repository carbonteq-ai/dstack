from unittest.mock import patch

import pytest

from dstack._internal.core.backends.runpod.configurator import RunpodConfigurator
from dstack._internal.core.backends.runpod.models import RunpodBackendConfigWithCreds, RunpodCreds
from dstack._internal.core.errors import BackendInvalidCredentialsError, ConfigurationError
from dstack._internal.core.models.provisioning_preconditions import (
    HTTPBearerCredentials,
    HTTPImageReadinessConfig,
)


class TestRunpodConfigurator:
    def test_validate_config_valid(self):
        config = RunpodBackendConfigWithCreds(
            creds=RunpodCreds(api_key="valid"),
        )
        with patch(
            "dstack._internal.core.backends.runpod.api_client.RunpodApiClient.validate_api_key"
        ) as validate_mock:
            validate_mock.return_value = True
            RunpodConfigurator().validate_config(config, default_creds_enabled=True)

    def test_validate_config_invalid_creds(self):
        config = RunpodBackendConfigWithCreds(
            creds=RunpodCreds(api_key="invalid"),
        )
        with (
            patch(
                "dstack._internal.core.backends.runpod.api_client.RunpodApiClient.validate_api_key"
            ) as validate_mock,
            pytest.raises(BackendInvalidCredentialsError) as exc_info,
        ):
            validate_mock.return_value = False
            RunpodConfigurator().validate_config(config, default_creds_enabled=True)
        assert exc_info.value.fields == [["creds", "api_key"]]

    @pytest.mark.parametrize("include_config,include_credentials", [(True, False), (False, True)])
    def test_validate_config_requires_matching_precondition_credentials(
        self, include_config: bool, include_credentials: bool
    ):
        config = RunpodBackendConfigWithCreds(
            provisioning_precondition=(
                HTTPImageReadinessConfig(url="http://controller.local/v1/publications")
                if include_config
                else None
            ),
            creds=RunpodCreds(
                api_key="valid",
                provisioning_precondition=(
                    HTTPBearerCredentials(bearer_token="secret") if include_credentials else None
                ),
            ),
        )

        with pytest.raises(ConfigurationError):
            RunpodConfigurator().validate_config(config, default_creds_enabled=True)

    def test_backend_resolves_guard_without_exposing_token_in_public_config(self):
        config = RunpodBackendConfigWithCreds(
            provisioning_precondition=HTTPImageReadinessConfig(
                url="http://controller.local/v1/publications"
            ),
            creds=RunpodCreds(
                api_key="valid",
                provisioning_precondition=HTTPBearerCredentials(bearer_token="secret"),
            ),
        )
        configurator = RunpodConfigurator()
        with patch(
            "dstack._internal.core.backends.runpod.api_client.RunpodApiClient.validate_api_key",
            return_value=True,
        ):
            configurator.validate_config(config, default_creds_enabled=True)
        record = configurator.create_backend("project", config)
        public = configurator.get_backend_config_without_creds(record)
        backend = configurator.get_backend(record)

        assert "secret" not in public.json()
        resolved = backend.get_image_readiness_precondition()
        assert resolved is not None
        assert resolved.credentials.bearer_token == "secret"
