# CarbonTeq dstack fork ledger

## Status

Published candidate branch. The working tree is based on upstream dstack
`0.20.29` at commit `2f9618f4d521140350efd1b344412d122c1e0322`.
`origin` points to `carbonteq-ai/dstack` and `upstream` points to
`dstackai/dstack`. Consumers may pin only a published CarbonTeq commit by full
SHA. Commit `a73c3314ab54cbe0e6056f6dad2e33e173596be6` is the currently qualified
server/runner/shim release.

Published branch `codex/registry-default-auth` adds the exact-host registry
credential and live RunPod GPU-offer behavior below on top of commit
`275b81bc725967c8925b5b12d96500dc60a45370`. The published pre-start regional
failover implementation is commit `e9d74b0cfd330500879946141469313e46de2e7d`.
The bounded retry-budget, region-cooldown, and persisted managed-storage
rotation implementation is published and deployed at commit
`a73c3314ab54cbe0e6056f6dad2e33e173596be6`.

## Maintained delta

### Apply server registry credentials to an explicit exact-match host

Posttrain submits a fully qualified, digest-pinned canonical image. Upstream's
`apply_server_docker_defaults` returns before applying server-owned credentials
whenever an image already contains a registry host, so provider-native pulls
and the runner receive no authentication for that private canonical registry.

The candidate delta applies `DSTACK_SERVER_DEFAULT_DOCKER_REGISTRY_USERNAME`
and `DSTACK_SERVER_DEFAULT_DOCKER_REGISTRY_PASSWORD` when, and only when, the
explicit image registry exactly equals `DSTACK_SERVER_DEFAULT_DOCKER_REGISTRY`
and the run did not supply explicit auth. It does not rewrite the image and
does not recognize prefixes, suffixes, or port-mismatched hosts. Existing
unqualified-image behavior and explicit run-auth precedence remain unchanged.

Regression coverage in
`src/tests/_internal/server/services/test_docker.py` includes exact match,
port mismatch, malicious prefix/suffix mismatch, explicit-auth precedence,
incomplete server credentials, and the existing unqualified-image cases.

### Resolve RunPod GPU spot offers from live capacity

The gpuhunt offline RunPod catalog is still useful for normalized hardware,
CPU and cluster shapes, and on-demand baseline pricing, but its current
published rows contain no spot offers. Treating every offline row as available
also cannot represent RunPod's volatile stock. This prevented dstack from
planning an interruptible RunPod Pod even when RunPod's live GraphQL API
reported capacity and a current spot price.

The candidate delta keeps the offline catalog for on-demand, CPU, and cluster
planning. A non-multinode GPU request that permits spot now queries RunPod's
live provider for only the requested GPU count and allowed locations, filters
Community Cloud unless configured, and converts the currently stocked rows
through dstack's existing requirement and offer normalization. The final Pod
creation mutation remains the authoritative capacity check because capacity
can disappear after discovery; normal dstack retry behavior handles that race.

Regression coverage in
`src/tests/_internal/core/backends/runpod/test_compute.py` verifies bounded
Secure Cloud discovery, live spot conversion, and preservation of the offline
on-demand path. A live read-only check returned current RTX PRO 6000 and A100
80 GB Secure Cloud spot rows in approximately six seconds.

Infrastructure may additionally set `minimum_stock_status` to `low`, `medium`,
or `high`. The upstream-compatible default remains `low`; CarbonTeq production
sets `medium`, making Low rows ineligible rather than merely ranking them after
stronger stock. This provider policy is independent of workload GPU and price
requirements and is covered by a focused rejection test.

### Select managed run storage from an infrastructure region pool

Managed RunPod storage may now omit a fixed data center when the backend owns
an ordered Secure Cloud region pool. Before creating the run-scoped network
volume, dstack evaluates live offers for the actual job requirements and picks
the lowest-priced eligible offer across the configured regions. Configuration
order is only a deterministic tie-breaker. The created volume then pins every
attempt of that run to the selected data center. Fixed-region configuration
remains supported for compatibility.

RunPod's live inventory can advertise a GPU that disappears before Pod
creation, and a region can also reject network-volume creation transiently.
Before any attempt has provisioned, dstack now records the failed region,
deletes the still-empty managed volume through the existing volume pipeline,
and reuses its logical volume row in the next-cheapest eligible region. Once
any job submission has provisioning data or the volume has an attachment,
regional rotation is forbidden and every interruption retry remains pinned to
the checkpoint-bearing volume. Each failed region enters a ten-minute
cooldown. If every eligible region is cooling down, the submission remains
pending; expired regions become eligible again and are compared by live price.

This keeps regional policy in infrastructure configuration while workload
clients specify only resource, spot, and price requirements. Focused tests
cover configuration validation, lowest-price selection, empty-volume rotation,
post-provision pinning, row reuse, and the legacy fixed-region path.

The submitted-job pipeline recognizes the exact run-owned mount after it has
been persisted into the run specification; arbitrary user volumes still opt
out. A no-capacity result can rotate only the regional volume that was active
when that result was recorded, so a failure from the previous region cannot
immediately evict its replacement. Live qualification on 2026-08-30 rotated an
empty `CA-MTL-3` volume to `US-WA-1`, honored both ten-minute cooldowns, then
recreated `CA-MTL-3` and made a fresh allocation attempt before responding to
the new provider no-capacity result.

### Bound capacity admission and interruption recovery independently

The upstream retry profile has one duration for all retry events and measures
interruption time from the latest provisioned submission. A successful spot
replacement therefore resets the interruption clock, while deleted historical
submissions can erase useful attempt evidence.

The candidate keeps the existing `duration` field as a compatibility fallback
and adds optional `duration_by_event` and `max_attempts_by_event` maps. Accepted
retry actions update a compact `runs.retry_state` record containing each
event's total attempts and first-event timestamp. The state is independent of
job-submission retention. Posttrain configures:

- `no-capacity`: 24 hours from initial submission;
- `interruption`: two hours from the first interruption, never reset;
- at most five interruption recoveries; and
- no retry for arbitrary workload errors.

Pending resubmissions retain the upstream exponential sequence (15 seconds,
30 seconds, one minute, two minutes, five minutes, then a ten-minute base cap)
and apply stable per-run, per-attempt jitter in the range of minus to plus 20
percent. Stable jitter prevents a polling cycle from moving its own deadline.

### Keep environment values out of diagnostic logs

The runner previously attached the complete `cmd.Env` list to its `Starting
exec` trace event. A server-side diagnostic log request could therefore expose
provider, registry, tracking, and workload credentials even though those
values were supplied through protected configuration.

The candidate now emits only sorted environment variable names. Values never
enter the trace event. `TestEnvNames_DoesNotExposeValues` covers secrets,
ordinary values, values containing additional equals signs, and malformed
entries. It is included in the published candidate and matching immutable
component builds.

### Honor bounded task stop duration

Upstream resolves `stop_duration` in the server job model but omits it from the
runner payload. The Go runner consequently uses a fixed ten-second wait, while
the server independently schedules container removal after ten seconds. A
five-minute job setting therefore gives a training process only about ten
seconds to finalize Trackio evidence and checkpoints.

The candidate delta:

- includes `stop_duration` in
  `src/dstack/_internal/server/schemas/runner.py`;
- derives the server removal deadline from the resolved job setting in
  `src/dstack/_internal/server/background/pipeline_tasks/jobs_terminating.py`;
- adds the nullable field to
  `runner/internal/runner/schemas/schemas.go`;
- derives the Go command wait delay from that field and preserves zero as
  immediate termination in
  `runner/internal/runner/executor/executor.go`;
- rejects `stop_duration: off` before task submission in
  `src/dstack/_internal/server/services/jobs/configurators/task.py`;
- retains a 300-second fallback for legacy stored jobs that predate the
  bounded validation; and
- leaves the eventual shim timeout at zero because the outer grace interval
  has already elapsed.

Regression coverage is in:

- `src/tests/_internal/server/services/runner/test_client.py`;
- `src/tests/_internal/server/background/pipeline_tasks/test_terminating_jobs.py`;
- `src/tests/_internal/server/services/jobs/configurators/test_task.py`;
- `runner/internal/runner/schemas/schemas_test.go`; and
- `runner/internal/runner/executor/executor_test.go`.

The maintained behavior is finite and zero stop duration for task workloads.
Unbounded `off` is intentionally unsupported until the terminating pipeline
can continue polling runner state without repeatedly initiating termination.

### Deliver cancellation to the job's process group

A bounded stop duration is useless if the workload never learns it should stop.
Upstream signals only `cmd.Process`, which is the shell that
`JobConfigurator._commands` builds (`/bin/sh -i -c "<commands>"`), and
`startCommand()` starts that shell as a session leader with the pty as its
controlling terminal. The shell therefore enables job control, places the
workload in its own process group, and makes that group the terminal's
foreground group. An interactive shell neither dies on nor forwards the
interrupt, so the workload never observed cancellation: it kept running for the
whole grace period and was then destroyed by the container hard kill, losing any
chance to finalize its own evidence.

Measured on a pty reproduction of the runner topology: with the workload under
`/bin/sh -i -c`, the shell's process group and the terminal's foreground
process group are different, signalling the shell's pid leaves the workload
running, and signalling the foreground group interrupts it and then lets the
shell exit normally.

The candidate delta in `runner/internal/runner/executor/executor.go`:

- publishes the job pty so cancellation can resolve the terminal's foreground
  process group through `TIOCGPGRP`;
- delivers the graceful interrupt to that process group, falling back to the
  command's own process group and finally to the command itself;
- deliberately leaves the shell unsignalled on the graceful path so it stays
  alive to wait for the workload, because killing it would let the runner treat
  the command as finished while the workload is still shutting down, truncating
  the stop duration; and
- kills both the workload group and the command on the zero stop-duration path,
  where no orderly shutdown is expected.

Regression coverage is
`TestExecutor_CancelReachesJobUnderInteractiveShell` in
`runner/internal/runner/executor/executor_test.go`, which reproduces the
production interactive-shell entrypoint and asserts that the workload runs its
own interrupt trap.

`TestExecutor_MaxDuration` previously asserted the error text `killed`. That
encoded the defect: the workload ignored the graceful signal and survived until
the hard kill. It now asserts the actual contract, that the job is terminated
for exceeding its max duration.

Known bound: after `WaitDelay` elapses Go kills only the command, so a workload
that ignores the interrupt for the entire stop duration can outlive the runner's
own kill until the shim's container stop removes it. That is acceptable because
both deadlines derive from the same stop duration.

## Compatibility and release

Build the server, runner, and shim from the same fork commit and give the
runner/shim one matching component version. A mixed rollout is unsafe:

- a patched server with an old runner still gives the process ten seconds;
- a patched runner with an old server receives no bounded field; and
- a new component version is required for dstack worker reconciliation to
  install the binaries.

The supported production path is dstack tasks on Linux AMD64 SSH-fleet workers
using Docker. The runner sends the interrupt to the launched process, so the
job shell must `exec` the stable worker command or forward signals.

## Validation

The candidate passed:

```text
83 affected Python tests passed, 21 PostgreSQL variants skipped
Go runner schema and executor packages passed
Ruff check and format check passed
git diff --check passed
```

The published exact-host credential delta additionally passes 38 focused
Docker-default and job-service tests plus Ruff and `git diff --check`.
The published RunPod live-offer delta passes all seven RunPod backend tests,
including its three new compute tests, plus Ruff and `git diff --check`.
The unpublished diagnostic-redaction successor passes the complete Go executor
package under Go 1.25, including the new no-values regression test.
The provisioning-timeout successor passes twelve focused RunPod configuration and
timeout tests plus Ruff; the broader submitted-job run retains its eight known
SQLite multinode/placement failures and adds no new failure.
The pre-start regional-failover successor passes five focused managed-storage
tests on SQLite (the five PostgreSQL variants skip when PostgreSQL is absent),
plus Ruff, format, and `git diff --check`. The complete submitted-job file still
has exactly the same eight known SQLite multinode/placement failures: 50 pass
and 58 PostgreSQL variants skip.
Resolving a fresh unpinned dev environment and running the two broader
submitted/running pipeline files produced 106 passes, 110 PostgreSQL skips,
and eight SQLite failures in unrelated multinode placeholder, cluster-lock,
and placement-group expectations. Re-run those gates in the pinned release
environment before publication; they are not represented as passing here.

The retry-budget and regional-cooldown successor passes 15 focused retry and
managed-storage tests with 14 PostgreSQL variants skipped, 36 profile/run-spec
compatibility tests with six PostgreSQL variants skipped, and the SQLite
migration test. The broader run/submitted pipeline has 94 passes, 101
PostgreSQL skips, and exactly the same eight unrelated SQLite multinode and
placement failures. No production or provider canary was run for this policy
follow-up.

The availability-first region successor passes all 30 selected RunPod backend,
managed-storage, rotation, and cooldown tests (with three PostgreSQL variants
skipped when PostgreSQL is absent), plus Ruff and `git diff --check`. An
authenticated read-only live-offer smoke reported `US-MD-1` A100 stock as
Medium and the remaining eligible US A100 regions as Low; the new ranking put
`US-MD-1` first despite its higher price. The subsequent provider canary proved
that `US-MD-1` is not network-volume-capable, deleted the failed empty logical
volume without a provider leak, and established that backend configuration must
contain the intersection of GPU stock and volume support. After restricting the
pool, r16 created `US-KS-2` storage, received no capacity, deleted it, and
rotated to `CA-MTL-3` with zero active Pods. Full CUDA execution remains open
because all currently eligible A100 stock is Low.

Before publication, repeat the Python suite with PostgreSQL enabled, build the
server and both binaries from the immutable candidate commit, and run a live
Docker cancellation whose handler takes more than ten seconds but less than
the configured stop duration. The release must prove the finalizer marker,
Trackio terminal state, container removal, and worker-idle state.

### Gate provider creation on immutable image readiness

A provider-native container backend pulls its image while creating the billed
resource. The source registry may accept an immutable manifest before a remote
replica has finished copying it, so creating the resource immediately can turn
normal mirror latency into a failed and billable job attempt.

The candidate adds an optional backend image-readiness precondition. RunPod is
the first backend to expose it. The server derives the repository and exact
`sha256` digest from the already-resolved job image, calls an authenticated
HTTP status endpoint after offer selection but before placement groups or
provider compute are created, and persists a secret-free snapshot on the job
submission. `waiting` remains pre-start and survives server restart; `ready`
permits the existing provider call; malformed images, contract/auth failures,
and bounded timeout fail as pre-start no-capacity outcomes. Backends without
the setting and jobs assigned to retained instances keep existing behavior.

The bearer token stays in the encrypted backend auth record. Persisted state
and API responses contain only backend, immutable image identity, public guard
settings, timing, state, and a safe result code. Focused tests cover absent
configuration, digest validation, pending and verified responses, restart via
the persisted snapshot, timeout, config mismatch, authorization rejection,
secret non-disclosure, and a pipeline proof that provider `run_job` remains
uncalled until the exact digest is verified.

### Make container provisioning timeout backend-configurable

RunPod pulls and unpacks the job image before dstack can reach its runner. The
upstream fixed 20-minute RunPod provisioning timeout is too short for the first
cold pull of the qualified 9 GB actual-job image, even though the provider Pod
and registry remain healthy.

The candidate adds an optional bounded `provisioning_timeout_seconds` RunPod
backend setting and persists its resolved value plus the provider-create time
in `JobProvisioningData` when the Pod is created. Both instance and job
readiness deadlines consume that persisted value, so a server restart cannot
silently revert an in-flight Pod to the default. Measuring from provider create
also prevents a pre-create image-readiness wait from consuming the pull budget.
Legacy attempts without the timestamp keep submission-time behavior. Omitting
the setting preserves the existing 20-minute timeout; configured values are
restricted to 10 through 60 minutes. Focused tests cover the bounds, public
non-secret configuration round trip, unchanged default, 30-minute override,
readiness-wait exclusion, and legacy fallback.

### Treat provider-side Pod disappearance as authoritative during provisioning

RunPod may reclaim an interruptible Pod after creation but before the runner
connects. The upstream adapter returns silently when `get_pod()` returns no
Pod, causing dstack to keep the logical job in `provisioning` until the full
timeout even though the provider resource is already gone.

The candidate now raises `ProvisioningError` as soon as RunPod reports the Pod
absent. The existing instance pipeline records the provider-side loss and
terminates the attempt immediately; a Pod that still exists but has no runtime
metadata continues waiting normally. Regression coverage in
`src/tests/_internal/core/backends/runpod/test_compute.py` covers both states.

### Treat provider-side Pod disappearance as authoritative while running

Runner transport failure is ambiguous for most backends, but a missing RunPod
Pod is authoritative for an interruptible attempt. After runner communication
fails, the running-job pipeline now asks the RunPod backend whether the Pod is
still present. Confirmed absence immediately classifies the attempt as an
interruption so the existing logical-run retry policy can proceed; provider API
errors and backends without authoritative observation retain the existing
transport timeout. The provider query is limited to spot attempts.

Regression coverage proves immediate interruption for confirmed absence and
preserves the fallback for all other backends.

### Own one managed network volume per RunPod spot run

RunPod backend configuration may opt single-node spot tasks into `run_storage`
with one Secure Cloud region, size, and mount path. Dstack creates one managed
network volume owned by the logical run, persists the generated mount in the
run and job specifications, and therefore reuses the same provider volume for
every retry. A unique nullable `volumes.run_id` is the ownership fence; explicit
volumes, on-demand jobs, services, multinode tasks, and other providers retain
their existing behavior.

When the logical run reaches a final status, dstack marks its owned volume for
the existing volume deletion pipeline. Provider deletion failures now remain
retryable and no longer emit a false `Volume deleted` event or set `deleted_at`.
The existing persisted volume row plus its terminal event is the cleanup
receipt, so this adds no second controller or cleanup ledger.

Before creating that managed volume, the successor cross-checks live spot
offers against RunPod's authenticated per-data-center GPU `stockStatus`.
Regions with blank or unreported stock are excluded, and eligible regions rank
`High`, `Medium`, then `Low` before price and backend configuration order. This
prevents a cheaper low-inventory region from owning the run's volume while a
stronger region is available. Because RunPod does not provide a reservable
capacity lease, provider allocation can still race after selection; the
existing bounded empty-volume rotation remains the recovery path.

RunPod may also return HTTP 500 after `createNetworkVolume` has actually
created the resource. The successor gives each logical-volume-and-region pair
a deterministic provider name, adopts an existing exact name before create,
and performs bounded read-after-error reconciliation before reporting an
ambiguous request as failed. Exact-name matches must also agree on region and
size. This keeps later cooldown retries capable of recovering a delayed
provider result instead of creating another unowned volume.

Focused RunPod configuration, running-job, submitted-job, run-termination, and
volume-deletion tests pass. The SQLite migration was exercised from an empty
database through head and back to its predecessor.
The ambiguous-create follow-up passes 52 focused RunPod, volume-pipeline,
managed-storage, rotation, and cooldown tests with 21 PostgreSQL variants
skipped when PostgreSQL is absent, plus Ruff and `git diff --check`.

## Rebase and retirement

Rebase from the exact upstream tag or commit, then inspect the runner payload
schema, task configurator, terminating pipeline, Go job schema, and executor
cancel path as one conflict-sensitive unit. Run all tests above plus the live
cancellation gate. Retire the fork only after an upstream release propagates
the same bounded value through both server and runner and passes the CarbonTeq
qualification unchanged.

The packaged `d2586c3871525e461bcbc442deaa511af2a87758` candidate additionally
passed a real RunPod Secure Cloud spot canary on an RTX PRO 6000 Blackwell
Server Edition. dstack observed submission through `done`, CUDA reported
97,887 MiB visible VRAM, and fleet deletion left the RunPod account with zero
active Pods.

Published fork commit: `d0268205c768a01c4573689cc68f041692e476b2` on branch
`codex/registry-default-auth`.
