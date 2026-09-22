"""An HTTP client for the sim controller.

The controller owns everything a scenario may want to vary — the catalogue,
stock, and faults — so this client, and the backend on top of it, stays a thin
translation layer that seldom needs to change. The wire format:

    GET    /offers        -> {"offers": [SimOffer, ...]}
    POST   /hosts         -> 201 SimHost | 409 {"error": "no_capacity", "message": ...}
    GET    /hosts/{id}    -> 200 SimHost | 404
    DELETE /hosts/{id}    -> 204 | 404
"""

from typing import List, Literal, Optional

import requests

from dstack._internal.core.errors import ComputeError, NoCapacityError
from dstack._internal.core.models.common import CoreModel

REQUEST_TIMEOUT = 10


class SimGpu(CoreModel):
    name: str
    memory_gib: float
    count: int
    vendor: str = "nvidia"


class SimOffer(CoreModel):
    name: str
    region: str
    price: float
    spot: bool
    cpus: int
    memory_gib: float
    disk_gib: float
    cpu_arch: str = "x86"
    gpu: Optional[SimGpu] = None
    availability: str = "available"
    """One of `InstanceAvailability`'s values; the controller decides what stock means."""


class SimHost(CoreModel):
    id: str
    status: Literal["starting", "running", "failed", "stopped"]
    hostname: Optional[str] = None
    ssh_port: Optional[int] = None
    message: Optional[str] = None


class SimControllerClient:
    def __init__(self, base_url: str):
        self._base_url = base_url.rstrip("/")
        self._session = requests.Session()

    def list_offers(self) -> List[SimOffer]:
        resp = self._request("GET", "/offers")
        return [SimOffer.parse_obj(o) for o in resp.json()["offers"]]

    def create_host(
        self,
        offer: str,
        region: str,
        instance_name: str,
        authorized_keys: List[str],
    ) -> SimHost:
        resp = self._request(
            "POST",
            "/hosts",
            json={
                "offer": offer,
                "region": region,
                "instance_name": instance_name,
                "authorized_keys": authorized_keys,
            },
            allow=(409,),
        )
        if resp.status_code == 409:
            raise NoCapacityError(_error_message(resp))
        return SimHost.parse_obj(resp.json())

    def get_host(self, host_id: str) -> Optional[SimHost]:
        resp = self._request("GET", f"/hosts/{host_id}", allow=(404,))
        if resp.status_code == 404:
            return None
        return SimHost.parse_obj(resp.json())

    def delete_host(self, host_id: str) -> None:
        self._request("DELETE", f"/hosts/{host_id}", allow=(404,))

    def _request(self, method: str, path: str, allow: tuple = (), **kwargs) -> requests.Response:
        try:
            resp = self._session.request(
                method, self._base_url + path, timeout=REQUEST_TIMEOUT, **kwargs
            )
        except requests.RequestException as e:
            raise ComputeError(f"sim controller unreachable: {e}") from e
        if resp.status_code in allow:
            return resp
        if not resp.ok:
            raise ComputeError(
                f"sim controller {method} {path} -> {resp.status_code}: {_error_message(resp)}"
            )
        return resp


def _error_message(resp: requests.Response) -> str:
    try:
        return str(resp.json().get("message") or resp.text)
    except ValueError:
        return resp.text
