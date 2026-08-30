# CarbonTeq dstack MVP

A two-machine prototype of the fork: the dstack server on a Dokploy VM, and one
Unraid VM as the SSH-fleet worker. It is a scale model of the `ai-infra`
production topology, with the release contract kept faithful and the operational
machinery (registry, internal CA, Ansible, leases, receipts, rollback) dropped.

## Why the server is built at all

The published `dstackai/dstack` image contains neither half of the fork delta,
which spans the Python server *and* the Go runner/shim. A server-only patch is
not enough: the graceful-stop deadline is serialized by the server and enforced
by the runner. So one commit produces one version, one server image, and two
binaries.

`Dockerfile` follows the production pattern — start from the digest-pinned
upstream image and replace only the project wheel, so the dependency graph stays
upstream's immutable one. It differs in preserving the upstream web UI across
the swap; a source-built wheel has no statics, so a plain `--reinstall` would
leave the server with no dashboard.

## Layout

| Machine | Runs |
| --- | --- |
| Dokploy VM | `postgres`, `server`, `binaries` |
| Worker VM | Docker, sshd; dstack installs the shim and runner itself |
| Your laptop | the `dstack` CLI |

Two flows, in opposite directions: the **server dials out to the worker on port
22** (everything else is tunnelled inside that connection), and the **worker
fetches the fork binaries from `binaries` over HTTP**.

## 1. Prepare the worker VM

Ubuntu, 2 vCPU / 4 GB is enough for a CPU-only cancellation test.

```
sudo apt update && sudo apt install -y docker.io openssh-server sudo
sudo useradd -m -s /bin/bash dstack && sudo usermod -aG docker dstack
echo 'dstack ALL=(ALL) NOPASSWD:ALL' | sudo tee /etc/sudoers.d/dstack
```

Add your public key to `/home/dstack/.ssh/authorized_keys`, and confirm
`AllowTcpForwarding yes` in `/etc/ssh/sshd_config` (the Ubuntu default). dstack
requires it for the tunnels.

## 2. Check the two network paths

Before deploying anything. From the Dokploy VM:

```
ping -c2 <WORKER_LAN_IP>
docker run --rm alpine nc -zv -w3 <WORKER_LAN_IP> 22
```

If the ping fails it is the Unraid VM network mode, not Docker — put both VMs on
`br0`. If the ping passes and the container check fails, the likely cause is a
LAN in the `172.16–172.31.x.x` range colliding with Docker's bridge subnet.

## 3. Compute the version

```
./docker/server/carbonteq/version.sh
```

This is the `ai-infra` release contract: `<upstream tag>+carbonteq.g<12 commit
chars>`. It is valid PEP 440, and the `g` prefix stops an all-numeric commit
prefix being normalized as a numeric local-version component.

**Bump it on every rebuild.** The server installs a component onto a worker only
when the expected version differs from what that worker reports, so a stale
version means workers silently keep their old binaries.

## 4. Deploy on Dokploy

Compose application → this repo, branch `dstack-cp-mvp`, compose path
`docker/server/carbonteq/docker-compose.yml`. Environment:

```
DSTACK_POSTGRES_PASSWORD=<generated>
DSTACK_SERVER_ADMIN_TOKEN=<generated>
DSTACK_RELEASE_VERSION=<output of version.sh>
DSTACK_SERVER_URL=http://<dokploy-vm-lan-ip>:3001
DSTACK_BINARIES_URL=http://<dokploy-vm-lan-ip>:8080
```

Port 8080 must be reachable from the worker VM. Verify from the worker:

```
curl -sI "http://<dokploy-vm-lan-ip>:8080/<version>/binaries/dstack-runner-linux-amd64"
```

A 404 or timeout here means the rollout will silently never happen.

## 5. Enrol the worker

```
dstack project add --name main --url http://<dokploy-vm-lan-ip>:3001 --token <admin token>
dstack apply -f fleet.dstack.yml
```

```yaml
type: fleet
name: mvp-workers
ssh_config:
  user: dstack
  identity_file: ~/.ssh/id_rsa
  hosts:
    - <WORKER_LAN_IP>
```

## 6. Prove the fork is live

Adapted from `ai-infra`'s `jobs/cancellation-smoke/task.dstack.yml`, with the
GPU block and private image removed:

```yaml
type: task
name: cancellation-smoke
image: ubuntu:24.04
shell: /bin/bash
commands:
  - |
    echo cancellation-ready
    : > /tmp/graceful-stop.markers
    trap 'printf "%s\n" graceful-stop-start | tee /tmp/graceful-stop.markers; sleep 20; printf "%s\n" graceful-stop-complete | tee -a /tmp/graceful-stop.markers; sync; exit 0' INT TERM
    while true; do sleep 1 & wait $! || true; done
resources:
  cpu: 2..
  memory: 2GB..
fleets: [mvp-workers]
max_duration: 5m
stop_duration: 45s
retry: false
```

`dstack apply`, wait for `cancellation-ready`, then cancel. The handler sleeps
20 seconds — well past upstream's fixed ten-second kill. Seeing
`graceful-stop-complete` is the fork working. Being killed at ten seconds means
the worker is still on upstream binaries; check the binaries URL and that you
bumped the version.

## Notes

- The architecture is resolved from each *worker's* CPU, defaulting to `amd64`.
  `Dockerfile.binaries` builds only the Dokploy host's arch, which matches the
  fork's supported path (Linux AMD64). An arm64 worker would 404.
- This proves dstack component propagation, signal delivery, and stop grace. It
  does not prove the framework's Trackio finalization barrier, which has its own
  gate in the framework plan.
