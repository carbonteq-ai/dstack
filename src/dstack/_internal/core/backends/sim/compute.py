from collections.abc import Iterator
from typing import List, Optional

import gpuhunt

from dstack._internal.core.backends.base.backend import Compute
from dstack._internal.core.backends.base.compute import (
    generate_unique_instance_name,
    get_job_instance_name,
)
from dstack._internal.core.backends.base.offers import (
    catalog_item_to_offer,
    filter_offers_by_requirements,
)
from dstack._internal.core.backends.sim.client import SimControllerClient, SimOffer
from dstack._internal.core.backends.sim.models import SimConfig
from dstack._internal.core.errors import ProvisioningError
from dstack._internal.core.models.backends.base import BackendType
from dstack._internal.core.models.instances import (
    InstanceAvailability,
    InstanceConfiguration,
    InstanceOfferWithAvailability,
    SSHKey,
)
from dstack._internal.core.models.placement import PlacementGroup
from dstack._internal.core.models.resources import Memory, Range
from dstack._internal.core.models.runs import Job, JobProvisioningData, Requirements, Run
from dstack._internal.core.models.volumes import Volume
from dstack._internal.utils.common import get_current_datetime
from dstack._internal.utils.logging import get_logger

logger = get_logger(__name__)

MAX_INSTANCE_NAME_LEN = 60


class SimCompute(Compute):
    """A container backend, shaped like RunPod: `run_job` returns provisioning data
    with no hostname, and `update_provisioning_data` fills it in once the host
    answers. Offers are not cached: stock and faults change between scenarios, and
    a cached offer would hide both.
    """

    def __init__(self, config: SimConfig):
        super().__init__()
        self.config = config
        self.client = SimControllerClient(config.controller_url)

    def get_offers(
        self, requirements: Requirements, full_offers: bool
    ) -> Iterator[InstanceOfferWithAvailability]:
        offers = []
        for sim_offer in self.client.list_offers():
            offer = _sim_offer_to_offer(sim_offer, requirements)
            if offer is not None:
                offers.append(offer)
        # The same market, GPU, CPU, memory and price filter every gpuhunt-backed
        # backend goes through — which is the point: the sim must not have its own.
        return filter_offers_by_requirements(offers, requirements)

    def run_job(
        self,
        run: Run,
        job: Job,
        instance_offer: InstanceOfferWithAvailability,
        project_ssh_public_key: str,
        project_ssh_private_key: str,
        volumes: List[Volume],
        placement_group: Optional[PlacementGroup],
        requirements: Requirements,
    ) -> JobProvisioningData:
        ssh_keys = [SSHKey(public=project_ssh_public_key.strip())]
        if run.run_spec.ssh_key_pub is not None:
            ssh_keys.append(SSHKey(public=run.run_spec.ssh_key_pub.strip()))
        instance_config = InstanceConfiguration(
            project_name=run.project_name,
            instance_name=get_job_instance_name(run, job),
            ssh_keys=ssh_keys,
            user=run.user,
        )
        host = self.client.create_host(
            offer=instance_offer.instance.name,
            region=instance_offer.region,
            instance_name=generate_unique_instance_name(
                instance_config, max_length=MAX_INSTANCE_NAME_LEN
            ),
            authorized_keys=instance_config.get_public_keys(),
        )
        return JobProvisioningData(
            backend=instance_offer.backend,
            instance_type=instance_offer.instance,
            instance_id=host.id,
            hostname=None,
            internal_ip=None,
            region=instance_offer.region,
            price=instance_offer.price,
            username="root",
            ssh_port=None,
            dockerized=False,
            ssh_proxy=None,
            backend_data=None,
            provisioning_timeout_seconds=self.config.provisioning_timeout_seconds,
            provisioning_started_at=get_current_datetime(),
        )

    def update_provisioning_data(
        self,
        provisioning_data: JobProvisioningData,
        project_ssh_public_key: str,
        project_ssh_private_key: str,
    ):
        host = self.client.get_host(provisioning_data.instance_id)
        if host is None:
            raise ProvisioningError(
                f"sim host {provisioning_data.instance_id} no longer exists during provisioning"
            )
        if host.status == "failed":
            raise ProvisioningError(
                f"sim host {provisioning_data.instance_id} failed: {host.message or 'no reason'}"
            )
        if host.status != "running" or host.hostname is None or host.ssh_port is None:
            return
        provisioning_data.hostname = host.hostname
        provisioning_data.ssh_port = host.ssh_port

    def is_instance_present(
        self,
        instance_id: str,
        region: str,
        backend_data: Optional[str] = None,
    ) -> Optional[bool]:
        # Mirrors RunPod's delta: a host the controller no longer knows is absent,
        # which is how a spot interruption is confirmed at once.
        return self.client.get_host(instance_id) is not None

    def terminate_instance(
        self, instance_id: str, region: str, backend_data: Optional[str] = None
    ):
        self.client.delete_host(instance_id)


def _sim_offer_to_offer(
    sim_offer: SimOffer, requirements: Requirements
) -> Optional[InstanceOfferWithAvailability]:
    gpu = sim_offer.gpu
    item = gpuhunt.CatalogItem(
        instance_name=sim_offer.name,
        location=sim_offer.region,
        price=sim_offer.price,
        cpu=sim_offer.cpus,
        memory=sim_offer.memory_gib,
        gpu_count=gpu.count if gpu else 0,
        gpu_name=gpu.name if gpu else None,
        gpu_memory=gpu.memory_gib if gpu else None,
        gpu_vendor=gpuhunt.AcceleratorVendor.cast(gpu.vendor) if gpu else None,
        spot=sim_offer.spot,
        disk_size=sim_offer.disk_gib,
        cpu_arch=gpuhunt.CPUArchitecture.cast(sim_offer.cpu_arch),
        provider=BackendType.SIM.value,
        flags=[],
    )
    disk = Memory.parse(f"{sim_offer.disk_gib}GB")
    offer = catalog_item_to_offer(
        backend=BackendType.SIM,
        item=item,
        requirements=requirements,
        configurable_disk_size=Range[Memory](min=disk, max=disk),
    )
    if offer is None:
        return None
    try:
        availability = InstanceAvailability(sim_offer.availability)
    except ValueError:
        logger.warning(
            "sim offer %s has unknown availability %r", sim_offer.name, sim_offer.availability
        )
        availability = InstanceAvailability.UNKNOWN
    return offer.with_availability(availability=availability)
