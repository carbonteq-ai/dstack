import uuid
from unittest.mock import MagicMock, patch

import gpuhunt
import pytest
import requests
from gpuhunt.providers.runpod import RunpodProvider

from dstack._internal.core.backends.runpod.compute import (
    RunpodCompute,
    RunpodOfferBackendData,
    _get_runpod_volume_name,
    _RunpodLiveGPUProvider,
)
from dstack._internal.core.backends.runpod.models import (
    RunpodAPIKeyCreds,
    RunpodConfig,
)
from dstack._internal.core.errors import ProvisioningError
from dstack._internal.core.models.resources import ResourcesSpec
from dstack._internal.core.models.runs import Requirements
from dstack._internal.core.models.volumes import RunpodVolumeConfiguration


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
        patch.object(
            compute.api_client,
            "get_data_center_gpu_availability",
            return_value={"EU-CZ-1": {"NVIDIA RTX PRO 6000 Blackwell Server Edition": "Medium"}},
        ),
    ):
        offers = compute.get_offers_by_requirements(requirements)

    assert len(offers) == 1
    assert offers[0].backend.value == "runpod"
    assert offers[0].region == "EU-CZ-1"
    assert offers[0].price == 2.09
    assert offers[0].instance.resources.spot is True
    assert offers[0].instance.resources.gpus[0].name == "RTXPRO6000"
    assert offers[0].instance.resources.gpus[0].memory_mib == 96 * 1024
    assert RunpodOfferBackendData.parse_obj(offers[0].backend_data).stock_status == "Medium"


def test_spot_gpu_offers_accept_full_offers_argument():
    compute = RunpodCompute(
        RunpodConfig(creds=RunpodAPIKeyCreds(api_key="secret"), community_cloud=False)
    )
    requirements = Requirements(
        resources=ResourcesSpec(gpu="A100:1", disk="20GB"),
        spot=True,
    )

    with (
        patch.object(RunpodProvider, "__init__", return_value=None),
        patch.object(_RunpodLiveGPUProvider, "get", return_value=[]),
        patch.object(
            compute.api_client,
            "get_data_center_gpu_availability",
            return_value={},
        ),
    ):
        assert compute.get_offers_by_requirements(requirements, full_offers=True) == []


def test_spot_gpu_offers_reject_regions_without_reported_stock():
    raw_offer = gpuhunt.RawCatalogItem(
        instance_name="NVIDIA A100 80GB PCIe",
        location="EUR-IS-1",
        price=1.89,
        cpu=16,
        memory=125,
        gpu_vendor="nvidia",
        gpu_count=1,
        gpu_name="A100",
        gpu_memory=80,
        spot=True,
        disk_size=None,
    )
    compute = RunpodCompute(
        RunpodConfig(creds=RunpodAPIKeyCreds(api_key="secret"), community_cloud=False)
    )
    requirements = Requirements(
        resources=ResourcesSpec(gpu="A100:1", disk="20GB"),
        spot=True,
    )

    with (
        patch.object(RunpodProvider, "__init__", return_value=None),
        patch.object(_RunpodLiveGPUProvider, "get", return_value=[raw_offer]),
        patch.object(
            compute.api_client,
            "get_data_center_gpu_availability",
            return_value={"EUR-IS-1": {"NVIDIA A100 80GB PCIe": ""}},
        ),
    ):
        assert compute.get_offers_by_requirements(requirements) == []


def test_spot_gpu_offers_respect_backend_minimum_stock_status():
    raw_offer = gpuhunt.RawCatalogItem(
        instance_name="NVIDIA A100 80GB PCIe",
        location="US-KS-2",
        price=1.39,
        cpu=16,
        memory=125,
        gpu_vendor="nvidia",
        gpu_count=1,
        gpu_name="A100",
        gpu_memory=80,
        spot=True,
        disk_size=None,
    )
    compute = RunpodCompute(
        RunpodConfig(
            creds=RunpodAPIKeyCreds(api_key="secret"),
            community_cloud=False,
            minimum_stock_status="medium",
        )
    )
    requirements = Requirements(
        resources=ResourcesSpec(gpu="A100:1", disk="20GB"),
        spot=True,
    )

    with (
        patch.object(RunpodProvider, "__init__", return_value=None),
        patch.object(_RunpodLiveGPUProvider, "get", return_value=[raw_offer]),
        patch.object(
            compute.api_client,
            "get_data_center_gpu_availability",
            return_value={"US-KS-2": {"NVIDIA A100 80GB PCIe": "Low"}},
        ),
    ):
        assert compute.get_offers_by_requirements(requirements) == []


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


@pytest.mark.parametrize(("pod", "expected"), [({"id": "pod-1"}, True), (None, False)])
def test_instance_presence_comes_from_runpod(pod, expected):
    compute = RunpodCompute(RunpodConfig(creds=RunpodAPIKeyCreds(api_key="secret")))
    compute.api_client.get_pod = MagicMock(return_value=pod)

    assert compute.is_instance_present("pod-1", "US-WA-1") is expected
    compute.api_client.get_pod.assert_called_once_with("pod-1")


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


def _managed_volume():
    volume = MagicMock()
    volume.id = uuid.UUID("12345678-1234-5678-1234-567812345678")
    volume.name = "run-12345678123456781234567812345678"
    volume.project_name = "main"
    volume.configuration = RunpodVolumeConfiguration(
        name=volume.name,
        size="100GB",
        region="US-CA-2",
    )
    return volume


def test_volume_name_is_deterministic_per_logical_volume_and_region():
    volume = _managed_volume()

    first = _get_runpod_volume_name(volume, "US-CA-2")
    second = _get_runpod_volume_name(volume, "US-CA-2")
    other_region = _get_runpod_volume_name(volume, "US-WA-1")

    assert first == second
    assert first != other_region
    assert len(first) <= 60


def test_ambiguous_volume_create_adopts_the_provider_result():
    volume = _managed_volume()
    compute = RunpodCompute(RunpodConfig(creds=RunpodAPIKeyCreds(api_key="secret")))
    provider_name = _get_runpod_volume_name(volume, "US-CA-2")
    created = {
        "id": "volume-1",
        "name": provider_name,
        "size": 100,
        "dataCenter": {"id": "US-CA-2"},
    }
    compute.api_client.get_network_volumes_by_name = MagicMock(side_effect=[[], [], [created]])
    compute.api_client.create_network_volume = MagicMock(
        side_effect=requests.HTTPError("500 Server Error")
    )

    with patch("dstack._internal.core.backends.runpod.compute.time.sleep") as sleep:
        provisioning = compute.create_volume(volume)

    assert provisioning.volume_id == "volume-1"
    compute.api_client.create_network_volume.assert_called_once_with(
        name=provider_name,
        region="US-CA-2",
        size=100,
    )
    sleep.assert_called_once_with(1)


def test_volume_retry_adopts_an_existing_deterministic_provider_volume():
    volume = _managed_volume()
    compute = RunpodCompute(RunpodConfig(creds=RunpodAPIKeyCreds(api_key="secret")))
    provider_name = _get_runpod_volume_name(volume, "US-CA-2")
    compute.api_client.get_network_volumes_by_name = MagicMock(
        return_value=[
            {
                "id": "volume-1",
                "name": provider_name,
                "size": 100,
                "dataCenter": {"id": "US-CA-2"},
            }
        ]
    )
    compute.api_client.create_network_volume = MagicMock()

    provisioning = compute.create_volume(volume)

    assert provisioning.volume_id == "volume-1"
    compute.api_client.create_network_volume.assert_not_called()
