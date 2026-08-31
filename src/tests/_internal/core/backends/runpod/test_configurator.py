from unittest.mock import patch

import pytest
from pydantic import ValidationError

from dstack._internal.core.backends.runpod.configurator import RunpodConfigurator
from dstack._internal.core.backends.runpod.models import (
    RunpodBackendConfigWithCreds,
    RunpodCreds,
    RunpodRunStorageConfig,
)
from dstack._internal.core.errors import BackendInvalidCredentialsError, ConfigurationError
from dstack._internal.core.models.provisioning_preconditions import (
    HTTPBearerCredentials,
    HTTPImageReadinessConfig,
)


class TestRunpodConfigurator:
    @pytest.mark.parametrize(
        ("kwargs"),
        [
            {"region": "US", "size": "10GB", "path": "/workspace"},
            {"region": "US-WA-1", "size": "9GB", "path": "/workspace"},
            {"region": "US-WA-1", "size": "10GB", "path": "workspace"},
        ],
    )
    def test_run_storage_rejects_unsupported_provider_settings(self, kwargs):
        with pytest.raises(ValidationError):
            RunpodRunStorageConfig(**kwargs)

    def test_run_storage_is_retained_in_public_and_runtime_config(self):
        run_storage = RunpodRunStorageConfig(
            region="US-WA-1",
            size="10GB",
            path="/workspace",
        )
        config = RunpodBackendConfigWithCreds(
            creds=RunpodCreds(api_key="valid"),
            run_storage=run_storage,
        )
        configurator = RunpodConfigurator()
        with patch(
            "dstack._internal.core.backends.runpod.api_client.RunpodApiClient.validate_api_key",
            return_value=True,
        ):
            configurator.validate_config(config, default_creds_enabled=True)
        record = configurator.create_backend("project", config)

        assert configurator.get_backend_config_without_creds(record).run_storage == run_storage
        assert configurator.get_backend(record).config.run_storage == run_storage

    def test_run_storage_can_select_from_backend_region_pool(self):
        run_storage = RunpodRunStorageConfig(size="10GB", path="/workspace")
        config = RunpodBackendConfigWithCreds(
            creds=RunpodCreds(api_key="valid"),
            regions=["US-CA-2", "US-KS-2"],
            run_storage=run_storage,
        )

        assert config.run_storage == run_storage
        assert config.regions == ["US-CA-2", "US-KS-2"]

    @pytest.mark.parametrize("regions", [None, [], ["US"]])
    def test_pooled_run_storage_requires_secure_backend_regions(self, regions):
        with pytest.raises(ValidationError):
            RunpodBackendConfigWithCreds(
                creds=RunpodCreds(api_key="valid"),
                regions=regions,
                run_storage=RunpodRunStorageConfig(size="10GB", path="/workspace"),
            )

    @pytest.mark.parametrize("value", [599, 3601])
    def test_provisioning_timeout_is_bounded(self, value: int):
        with pytest.raises(ValidationError):
            RunpodBackendConfigWithCreds(
                creds=RunpodCreds(api_key="valid"),
                provisioning_timeout_seconds=value,
            )

    def test_provisioning_timeout_is_retained_in_public_config(self):
        config = RunpodBackendConfigWithCreds(
            creds=RunpodCreds(api_key="valid"),
            provisioning_timeout_seconds=1800,
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

        assert public.provisioning_timeout_seconds == 1800
        assert backend.config.provisioning_timeout_seconds == 1800

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
