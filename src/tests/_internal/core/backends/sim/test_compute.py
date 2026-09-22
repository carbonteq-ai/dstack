import os
import subprocess
import sys
from unittest.mock import MagicMock

import pytest
import requests_mock

from dstack._internal.core.backends.sim.compute import SimCompute
from dstack._internal.core.backends.sim.models import SimConfig
from dstack._internal.core.errors import ComputeError, NoCapacityError, ProvisioningError
from dstack._internal.core.models.backends.base import BackendType
from dstack._internal.core.models.instances import InstanceAvailability
from dstack._internal.core.models.resources import ResourcesSpec
from dstack._internal.core.models.runs import JobProvisioningData, Requirements

URL = "http://sim-controller:7070"

ON_DEMAND = {
    "name": "sim-cpu",
    "region": "sim-eu",
    "price": 0.25,
    "spot": False,
    "cpus": 8,
    "memory_gib": 64,
    "disk_gib": 200,
    "gpu": None,
}
SPOT_GPU = {
    "name": "sim-a100",
    "region": "sim-us",
    "price": 1.1,
    "spot": True,
    "cpus": 16,
    "memory_gib": 128,
    "disk_gib": 200,
    "gpu": {"name": "A100", "memory_gib": 80, "count": 1},
    "availability": "not_available",
}


@pytest.fixture
def compute() -> SimCompute:
    return SimCompute(SimConfig(controller_url=URL))


@pytest.fixture
def controller():
    with requests_mock.Mocker() as m:
        m.get(f"{URL}/offers", json={"offers": [ON_DEMAND, SPOT_GPU]})
        yield m


def _requirements(spot=None) -> Requirements:
    return Requirements(resources=ResourcesSpec(), spot=spot)


def _run_and_job():
    run = MagicMock()
    run.project_name = "main"
    run.user = "admin"
    run.run_spec.ssh_key_pub = "ssh-ed25519 USERKEY user"
    job = MagicMock()
    job.job_spec.job_name = "smoke-0-0"
    return run, job


def _jpd(instance_id="h1") -> JobProvisioningData:
    return JobProvisioningData(
        backend=BackendType.SIM,
        instance_type={
            "name": "sim-cpu",
            "resources": {"cpus": 8, "memory_mib": 65536, "gpus": [], "spot": False},
        },
        instance_id=instance_id,
        hostname=None,
        region="sim-eu",
        price=0.25,
        username="root",
        ssh_port=None,
        dockerized=False,
        backend_data=None,
        ssh_proxy=None,
    )


class TestGetOffers:
    def test_converts_the_catalogue(self, compute, controller):
        offers = {o.instance.name: o for o in compute.get_offers(_requirements(), False)}
        assert set(offers) == {"sim-cpu", "sim-a100"}
        cpu = offers["sim-cpu"]
        assert cpu.backend == BackendType.SIM
        assert cpu.region == "sim-eu"
        assert cpu.price == 0.25
        assert cpu.instance.resources.cpus == 8
        assert cpu.instance.resources.memory_mib == 64 * 1024
        assert cpu.instance.resources.disk.size_mib == 200 * 1024
        assert cpu.instance.resources.spot is False
        assert cpu.availability == InstanceAvailability.AVAILABLE
        gpu = offers["sim-a100"]
        assert [g.name for g in gpu.instance.resources.gpus] == ["A100"]
        assert gpu.instance.resources.gpus[0].memory_mib == 80 * 1024
        assert gpu.availability == InstanceAvailability.NOT_AVAILABLE

    @pytest.mark.parametrize(("spot", "names"), [(True, {"sim-a100"}), (False, {"sim-cpu"})])
    def test_the_market_filter_is_gpuhunts(self, compute, controller, spot, names):
        offers = compute.get_offers(_requirements(spot=spot), False)
        assert {o.instance.name for o in offers} == names

    def test_an_unknown_availability_is_unknown_not_an_error(self, compute):
        with requests_mock.Mocker() as m:
            m.get(f"{URL}/offers", json={"offers": [{**ON_DEMAND, "availability": "odd"}]})
            (offer,) = compute.get_offers(_requirements(), False)
        assert offer.availability == InstanceAvailability.UNKNOWN

    def test_offers_are_not_cached(self, compute, controller):
        list(compute.get_offers(_requirements(), False))
        list(compute.get_offers(_requirements(), False))
        assert controller.call_count == 2

    def test_an_unreachable_controller_is_a_compute_error(self, compute):
        with requests_mock.Mocker() as m:
            m.get(f"{URL}/offers", status_code=500, json={"message": "boom"})
            with pytest.raises(ComputeError, match="boom"):
                list(compute.get_offers(_requirements(), False))


class TestRunJob:
    def test_asks_for_a_host_and_returns_no_hostname(self, compute, controller):
        offer = next(
            o for o in compute.get_offers(_requirements(), False) if not o.instance.resources.spot
        )
        controller.post(f"{URL}/hosts", status_code=201, json={"id": "h1", "status": "starting"})
        run, job = _run_and_job()
        jpd = compute.run_job(
            run, job, offer, "ssh-rsa PROJECTKEY", "private", [], None, _requirements()
        )
        body = controller.last_request.json()
        assert body["offer"] == "sim-cpu"
        assert body["region"] == "sim-eu"
        assert body["authorized_keys"] == ["ssh-rsa PROJECTKEY", "ssh-ed25519 USERKEY user"]
        assert "smoke-0-0" in body["instance_name"]
        assert jpd.instance_id == "h1"
        assert jpd.hostname is None and jpd.ssh_port is None
        assert jpd.dockerized is False
        assert jpd.username == "root"
        assert jpd.price == 0.25

    def test_a_409_is_no_capacity(self, compute, controller):
        offer = next(iter(compute.get_offers(_requirements(), False)))
        controller.post(
            f"{URL}/hosts",
            status_code=409,
            json={"error": "no_capacity", "message": "stock exhausted"},
        )
        run, job = _run_and_job()
        with pytest.raises(NoCapacityError, match="stock exhausted"):
            compute.run_job(run, job, offer, "k", "p", [], None, _requirements())


class TestUpdateProvisioningData:
    def test_fills_in_the_address_once_running(self, compute):
        jpd = _jpd()
        with requests_mock.Mocker() as m:
            m.get(f"{URL}/hosts/h1", json={"id": "h1", "status": "starting"})
            compute.update_provisioning_data(jpd, "k", "p")
            assert jpd.hostname is None
            m.get(
                f"{URL}/hosts/h1",
                json={
                    "id": "h1",
                    "status": "running",
                    "hostname": "sim-host-h1",
                    "ssh_port": 10022,
                },
            )
            compute.update_provisioning_data(jpd, "k", "p")
        assert (jpd.hostname, jpd.ssh_port) == ("sim-host-h1", 10022)

    @pytest.mark.parametrize(
        "response",
        [
            {"status_code": 404, "json": {"message": "gone"}},
            {"json": {"id": "h1", "status": "failed", "message": "fault: provision_fail"}},
        ],
    )
    def test_a_failed_or_missing_host_is_a_provisioning_error(self, compute, response):
        with requests_mock.Mocker() as m:
            m.get(f"{URL}/hosts/h1", **response)
            with pytest.raises(ProvisioningError):
                compute.update_provisioning_data(_jpd(), "k", "p")


class TestPresenceAndTermination:
    def test_presence_is_whether_the_controller_knows_the_host(self, compute):
        with requests_mock.Mocker() as m:
            m.get(f"{URL}/hosts/h1", json={"id": "h1", "status": "running"})
            m.get(f"{URL}/hosts/h2", status_code=404, json={})
            assert compute.is_instance_present("h1", "sim-eu") is True
            assert compute.is_instance_present("h2", "sim-eu") is False

    def test_terminating_a_gone_host_is_silent(self, compute):
        with requests_mock.Mocker() as m:
            m.delete(f"{URL}/hosts/h1", status_code=404, json={})
            compute.terminate_instance("h1", "sim-eu")
            m.delete(f"{URL}/hosts/h2", status_code=204)
            compute.terminate_instance("h2", "sim-eu")


class TestTheGate:
    """The sim backend must never load on a server that did not ask for it."""

    @staticmethod
    def _available(enabled):
        env = {k: v for k, v in os.environ.items() if k != "DSTACK_SIM_ENABLED"}
        if enabled is not None:
            env["DSTACK_SIM_ENABLED"] = enabled
        code = (
            "from dstack._internal.core.backends.configurators import "
            "list_available_backend_types as l; print('sim' in [b.value for b in l()])"
        )
        out = subprocess.run(
            [sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True
        )
        return out.stdout.strip()

    @pytest.mark.parametrize(
        ("enabled", "expected"),
        [(None, "False"), ("0", "False"), ("true", "False"), ("1", "True")],
    )
    def test_only_dstack_sim_enabled_1_registers_it(self, enabled, expected):
        assert self._available(enabled) == expected

    def test_a_config_naming_sim_still_parses(self):
        # The models are always importable, so a disabled server refuses the
        # backend as unavailable rather than failing to parse its config.
        from dstack._internal.server.services.config import config_yaml_to_backend_config

        config = config_yaml_to_backend_config("type: sim\ncontroller_url: http://c:7070\n")
        assert config.type == "sim"
