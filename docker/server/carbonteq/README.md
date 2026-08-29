# Deploying the CarbonTeq dstack fork on Dokploy

Upstream's published image (`dstackai/dstack`) does not contain this fork. The
delta spans two artifacts — the Python server and the Go `dstack-runner` /
`dstack-shim` binaries — and `CARBONTEQ_FORK.md` requires both to come from the
same commit. A patched server with an old runner still gives a job ten seconds;
a patched runner with an old server receives no bounded field.

So the stack builds both, and serves the binaries over HTTP so the server can
install them onto SSH-fleet workers.

## Dokploy setup

1. **Create a Compose application** pointing at this repo, branch
   `dstack-cp-mvp`, compose path `docker/server/carbonteq/docker-compose.yml`.
2. **Domains** — two services need to be reachable:
   - `server` → port `3000`, your control-plane domain.
   - `binaries` → port `80`, a domain the **worker machines** can reach. Workers
     fetch the runner/shim from here; if it is not publicly resolvable the
     rollout silently never happens.
3. **Port `30022`** is raw SSH for the proxy. Traefik's HTTP routers cannot
   carry it, so it stays a published host port in the compose file. Open it in
   the host firewall.
4. **Set the environment variables** below in Dokploy's Environment tab.
5. Deploy. First build is slow (npm + Go + uv); later ones hit layer cache.

## Environment variables

### Required

| Variable | Example | Notes |
| --- | --- | --- |
| `DSTACK_POSTGRES_PASSWORD` | *(generated)* | Also used to build `DSTACK_DATABASE_URL`. |
| `DSTACK_SERVER_ADMIN_TOKEN` | *(uuid)* | Without it the token is regenerated and only printed to logs. |
| `DSTACK_SERVER_URL` | `https://dstack.example.com` | Public URL. Used in links the server hands out. |
| `DSTACK_COMPONENT_VERSION` | `0.20.29-ct1` | The fork's component version. Stamped into the binaries via `-X main.Version` and compared against what each worker reports. Bump it on every rebuild or workers keep their existing binaries. |
| `DSTACK_BINARIES_URL` | `https://dstack-bin.example.com` | Public base URL of the `binaries` service. No trailing slash. |
| `DSTACK_SSHPROXY_API_TOKEN` | *(secret)* | Shared between server and proxy. |
| `DSTACK_SERVER_SSHPROXY_ADDRESS` | `dstack.example.com:30022` | Address **clients** use to reach the proxy, not an internal one. |

### Optional

| Variable | Default | Notes |
| --- | --- | --- |
| `DSTACK_POSTGRES_USER` | `dstack` | |
| `DSTACK_POSTGRES_DB` | `dstack` | |
| `DSTACK_SERVER_LOG_LEVEL` | `INFO` | `DEBUG` shows the runner-install decisions. |
| `DSTACK_SERVER_S3_BUCKET` / `DSTACK_SERVER_GCS_BUCKET` | — | External file storage. |
| `DSTACK_SERVER_CLOUDWATCH_LOG_GROUP` | — | External logs storage; otherwise logs live on the `server-data` volume. |
| `DSTACK_SENTRY_DSN` | — | |

### Set by the compose file — do not override

`DSTACK_RUNNER_VERSION`, `DSTACK_SHIM_VERSION`, `DSTACK_RUNNER_DOWNLOAD_URL`,
`DSTACK_SHIM_DOWNLOAD_URL` are derived from `DSTACK_COMPONENT_VERSION` and
`DSTACK_BINARIES_URL`. `DSTACK_SERVER_RELOAD_DISABLED` is pinned on.

### Do not set

**`DSTACK_VERSION`.** It is parsed as PEP 440 at import
(`_internal/utils/version.py`), so a value like `0.20.29-ct1` raises
`ValueError: Invalid version` and the server will not start. It also
short-circuits both runner and shim version resolution. Leave it unset — the
runner/shim vars above are read as raw strings and accept any label.

## Two things that will bite you

**Autoreload.** A source build leaves `version.py` at `0.0.0`, which resolves
`DSTACK_VERSION` to `None`, which turns uvicorn's autoreload on
(`cli/commands/server.py`). The compose file sets
`DSTACK_SERVER_RELOAD_DISABLED=1`; keep it.

**Architecture.** The server substitutes `{arch}` from each *worker's* CPU
architecture, defaulting to `amd64`. `Dockerfile.binaries` builds one arch — the
Dokploy host's. That matches the fork's supported path (Linux AMD64 SSH-fleet
workers). An arm64 worker would 404 on download; build and serve an arm64
binary too if you have one.

## Verifying the rollout

The server only installs a component when it knows an expected version *and*
that version differs from what the worker reports
(`background/pipeline_tasks/instances/check.py`). So:

1. `curl https://dstack-bin.example.com/0.20.29-ct1/binaries/dstack-runner-linux-amd64 -o /dev/null -w '%{http_code}\n'` → `200`.
2. Server logs at `DEBUG` should show `installing runner (no version) -> 0.20.29-ct1 from ...` per instance.
3. Run the live gate from `CARBONTEQ_FORK.md`: a task whose handler takes more
   than ten seconds but less than the configured `stop_duration`, cancelled
   mid-run. It must finalize rather than be killed at ten seconds.
