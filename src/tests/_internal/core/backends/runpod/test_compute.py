from unittest.mock import MagicMock, patch

import gpuhunt
import pytest
from gpuhunt.providers.runpod import RunpodProvider

from dstack._internal.core.backends.runpod.compute import (
    RunpodCompute,
    _RunpodLiveGPUProvider,
)
from dstack._internal.core.backends.runpod.models import RunpodAPIKeyCreds, RunpodConfig
from dstack._internal.core.errors import ProvisioningError
from dstack._internal.core.models.resources import ResourcesSpec
from dstack._internal.core.models.runs import Requirements


def test_live_provider_bounds_queries_to_requested_secure_location_and_gpu_count():
    variables = [
        {
            "lowestPriceInput": {
                "secureCloud": True,
                "dataCenterId": "EU-CZ-1",
                "countryCode": None,
                "gpuCount": 1,
            }
        },
        {
            "lowestPriceInput": {
                "secureCloud": True,
                "dataCenterId": "US-NC-2",
                "countryCode": None,
                "gpuCount": 2,
            }
        },
        {
            "lowestPriceInput": {
                "secureCloud": False,
                "dataCenterId": None,
                "countryCode": "US",
                "gpuCount": 1,
            }
        },
    ]
    with (
        patch.object(_RunpodLiveGPUProvider.__bases__[0], "__init__", return_value=None),
        patch.object(
            _RunpodLiveGPUProvider.__bases__[0], "_build_query_variables", return_value=variables
        ),
    ):
        provider = _RunpodLiveGPUProvider(
            gpu_counts=range(1, 2),
            regions=["EU-CZ-1"],
            allow_community_cloud=False,
        )
        assert provider._build_query_variables() == [variables[0]]


def test_spot_gpu_offers_come_from_live_runpod_capacity():
    raw_offer = gpuhunt.RawCatalogItem(
        instance_name="NVIDIA RTX PRO 6000 Blackwell Server Edition",
        location="EU-CZ-1",
        price=2.09,
        cpu=16,
        memory=125,
        gpu_vendor="nvidia",
        gpu_count=1,
        gpu_name="RTXPRO6000",
        gpu_memory=96,
        spot=True,
        disk_size=None,
    )
    compute = RunpodCompute(
        RunpodConfig(creds=RunpodAPIKeyCreds(api_key="secret"), community_cloud=False)
    )
    requirements = Requirements(
        resources=ResourcesSpec(gpu="RTXPRO6000:1", disk="20GB"),
        spot=True,
    )

    with (
        patch.object(RunpodProvider, "__init__", return_value=None),
        patch.object(_RunpodLiveGPUProvider, "get", return_value=[raw_offer]),
    ):
        offers = compute.get_offers_by_requirements(requirements)

    assert len(offers) == 1
    assert offers[0].backend.value == "runpod"
    assert offers[0].region == "EU-CZ-1"
    assert offers[0].price == 2.09
    assert offers[0].instance.resources.spot is True
    assert offers[0].instance.resources.gpus[0].name == "RTXPRO6000"
    assert offers[0].instance.resources.gpus[0].memory_mib == 96 * 1024


def test_on_demand_offers_keep_using_offline_catalog():
    compute = RunpodCompute(RunpodConfig(creds=RunpodAPIKeyCreds(api_key="secret")))
    requirements = Requirements(resources=ResourcesSpec(gpu="A100:1"), spot=False)

    with (
        patch(
            "dstack._internal.core.backends.runpod.compute.get_catalog_offers",
            return_value=[],
        ) as get_catalog_offers,
        patch.object(_RunpodLiveGPUProvider, "get") as get_live_offers,
    ):
        assert compute.get_offers_by_requirements(requirements) == []

    get_catalog_offers.assert_called_once()
    get_live_offers.assert_not_called()


def test_absent_pod_fails_provisioning_immediately():
    compute = RunpodCompute(RunpodConfig(creds=RunpodAPIKeyCreds(api_key="secret")))
    compute.api_client.get_pod = MagicMock(return_value=None)
    provisioning_data = MagicMock(instance_id="pod-1", hostname=None)

    with pytest.raises(ProvisioningError, match="no longer exists during provisioning"):
        compute.update_provisioning_data(provisioning_data, "public", "private")


def test_pod_without_runtime_keeps_waiting_for_provisioning():
    compute = RunpodCompute(RunpodConfig(creds=RunpodAPIKeyCreds(api_key="secret")))
    compute.api_client.get_pod = MagicMock(return_value={"runtime": None})
    provisioning_data = MagicMock(instance_id="pod-1", hostname=None)

    compute.update_provisioning_data(provisioning_data, "public", "private")

    assert provisioning_data.hostname is None
