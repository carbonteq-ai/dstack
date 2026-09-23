# CarbonTeq dstack fork ledger

## Status

Published candidate branch. The working tree is based on upstream dstack
`0.20.29` at commit `2f9618f4d521140350efd1b344412d122c1e0322`.
`origin` points to `carbonteq-ai/dstack` and `upstream` points to
`dstackai/dstack`. Consumers may pin only a published CarbonTeq commit by full
SHA. Commit `85cab941fb4f8e243c7014c278ea01705f89651e` is the currently qualified
server/runner/shim release; its merge migration, rolling worker gate, and
graceful-cancellation canary passed in production. A live read-only RunPod
offer query also passed after deployment.

Published branch `codex/registry-default-auth` adds the exact-host registry
credential and live RunPod GPU-offer behavior below on top of commit
`275b81bc725967c8925b5b12d96500dc60a45370`. The published pre-start regional
failover implementation is commit `e9d74b0cfd330500879946141469313e46de2e7d`.
The bounded retry-budget, region-cooldown, and persisted managed-storage
rotation implementation is published and deployed at commit
`a73c3314ab54cbe0e6056f6dad2e33e173596be6`.
The provider-configurable minimum stock policy is published and deployed at
commit `ae5dac6576b0f19b49e81b31781f3b9f14e95361`.
The candidate branch incorporates CarbonTeq default-branch commit
`b5ff8987f` through merge commit
`98c75b7f6136fb40420e58a6f3eed476f6aaa088`; this retains the maintained delta
while including the current gateway-replica and full-offer work.
The post-merge release successor joins the upstream gateway and CarbonTeq
run-lifecycle Alembic branches with one no-op merge revision. This keeps both
published migration histories intact while restoring a single `head` target.

## Purged from history

`docker/server/carbonteq/policy.yaml` was removed from every commit on this
branch on 2026-08-31, together with `policy/tests/test_shipped_config.py`, whose
fixtures read it.

`origin` is a **public** fork of `dstackai/dstack`. That file committed named
employees, their team assignments and their dollar budgets. It is operator data
and belongs in no public mirror under any structure. It was purged rather than
deleted in a later commit, because a deletion leaves the content in history and
the disclosure survives it.

The rewrite replaced head `69f8a7a4da3e`. Fifteen fork commits above the
upstream merge base became fourteen: `518f7f49` ("Assign the real users to team
projects in policy.yaml") touched nothing else and was pruned as empty. The
eight commits predating the file keep their original ids. Diffing the old head
against the new one shows those two files and nothing else.

Anyone holding the old branch must reset to the new head. Merging reintroduces
the file.

**This is not full remediation.** On GitHub the pre-rewrite commits remain
reachable by SHA, and a fork network shares an object pool, so they can also be
fetched through the parent repository. Only GitHub Support can make them
unreachable. Treat the names and the budgets as disclosed; no credential was
involved, so nothing requires rotation.

Do not reintroduce a policy file here. Policy is the control plane's under
ADR-020 — the schema and the deployed values live in the consumer repository,
and this tree carries only the deltas that cannot live outside dstack.

## Moved to the control-plane repository

`docker/server/carbonteq/docker-compose.yml`, `config.yml` and `README.md` left
this tree on 2026-08-31 for `app/deploy/dstack-server/` in the private control-plane
repository. ADR-024 draws the line at build artifact versus deployment topology:
those three describe our infrastructure, not dstack's build.

`Dockerfile`, `Dockerfile.binaries` and `version.sh` deliberately **stay**. The
fork patches the Python server *and* the Go runner, and only a build from this
tree can guarantee the two match — a mismatch is silent, not loud. Moving
`version.sh` out would mean passing a version in from outside, reintroducing the
manual bump this fork removed.

The compose file now builds with `context: ../../../.dstack-ref`, so it consumes
this tree as a submodule instead of being cloned with it. Two things follow, and
they are documented there rather than here: the deploy target must check
submodules out, and `version.sh`'s trick of reading ref metadata out of the build
context only works while `.dstack-ref/.git` is a directory rather than a
`gitdir:` pointer file.

Unlike the policy file above, these were deleted in an ordinary commit, not
purged. They carry no credentials; the deployment README does carry a username
and spend figures, which were redacted in the copy that moved and remain in this
branch's history. That was a deliberate scope decision, recorded in the consumer
repository's `harness/progress.md`.

## Deleted: the ctpolicy package

`policy/` — 1,949 lines of source and 1,782 of tests — was deleted on 2026-08-31
(task T3), and the `Dockerfile` no longer builds or installs its wheel.

It was built for a premise we reversed. `ctpolicy` assumed users keep the
official dstack CLI, so enforcement had to live inside the server. The control
plane is now the only client and nobody holds a dstack token, so enforcement
lives there instead (ADR-020). More decisively, `ctpolicy` resolves a run's team
from the dstack project name, and ADR-026 puts every run in one project under a
service credential — it would reject every submission. Incompatible, not merely
redundant.

Where the logic went: `config.py` and `windows.py` to the control plane's
`backend/policy/`; `usage.py` and the enforcer's snapshot loop to
`backend/usage/`; `plugin.py`'s decision table to `backend/admission/`. The
enforcer's *termination* backstop is deliberately not reproduced — ADR-022 clamps
`max_duration` instead of terminating. `cli.py` is replaced by `ctl quota`.

The one thing in the code that the design documents did not carry — that the
cloud rules must run *before* the duration clamp, because a run pinned to on-prem
cannot spend money and must not have its duration cut by a dollar budget — is now
recorded in the control plane's `docs/phase-2/04-accounting.md`.

Nothing here is a rebase surface: `policy/` was an additive package and touched
no upstream file. Recovering it needs no special measure — it is an ordinary
deletion, so the tree before this commit still has it.

## Maintained delta

### Keep the RunPod offer adapter compatible with the core offer contract

The core filtered-offer interface now passes a `full_offers` argument. The
RunPod override accepts that argument even though its live-capacity result does
not currently vary with the flag. Without this compatibility parameter every
RunPod offer lookup raised `TypeError` before capacity admission.

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

**A replacement's capacity wait belongs to the interruption budget** (control
plane C-67, 2026-09-15). The no-capacity budget is anchored once per run: at
initial submission, and after provisioning at its stored `first_at`, which is
never reset. So a spot run that waited for its first machine, or is on a later
recovery, had no wait left when its replacement found no offer after a reclaim.
It failed `retry_limit_exceeded` on the first empty lookup, which ended the
recovery this delta exists to allow. A reclaim is the provider taking capacity
back, so an empty first lookup is the likely case, not the rare one.

`_should_retry_job` now returns a `_RetryEvaluation` that separates the event it
records from the budget it checks. A `no-capacity` failure after a provisioned
submission is judged against `duration_for(interruption)`, measured from the
first interruption, when the run's `on_events` include `interruption` and its
`retry_state` has recorded one. It is still recorded, and capped, as a
`no-capacity` attempt. The interruption attempt count and cap do not move,
because an empty lookup creates no machine: counting lookups as recoveries would
end a run after two backoffs. The first start, runs that do not retry
`interruption`, and every other event keep the anchors above.

Under the reference configuration this shortens a replacement's wait. Before, it
was what remained of 24 hours since initial submission. Now it is what remains of
two hours since the first interruption. That is the stated meaning of "two hours
from the first interruption, never reset": no recovery starts after it. The
CarbonTeq control plane sends at most an hour for no-capacity and at most two for
interruption, and under the per-run anchor its replacements got no wait at all.

Touches `server/background/pipeline_tasks/runs/active.py` only:
`_should_retry_job`, `_is_retry_limit_exceeded` (it now takes the evaluation), the
single call site in `_analyze_active_run_replica`, and a new
`_replacement_budget_started_at`. Coverage is five tests in
`src/tests/_internal/server/background/pipeline_tasks/test_runs/test_active.py`,
beside `test_interruption_retry_budget_is_anchored_to_first_interruption`. They
cover the C-67 timeline recovering, a replacement past the interruption window
still failing, empty lookups not spending recoveries, and two cases that do not
change: the first start still bound by the no-capacity budget, and the per-run
anchor kept when `interruption` is not retried. The first three fail against
`30472de`.

*Rebase.* A hot pipeline file. Read `_should_retry_job`,
`_is_retry_limit_exceeded` and `_record_retry_events` as one unit. Upstream has
neither `retry_state` nor per-event budgets, so a conflict there means this whole
delta needs re-deriving, not only the replacement rule. *Retire* it with the
delta, or earlier if upstream lets a retry event be judged by another event's
budget.

### Wake a run waiting for capacity when an instance frees a block

Control plane D-49, 2026-09-16. The backoff above is the only thing that
decides when a run waiting for capacity tries again, and nothing shortens it.
A slot that frees just after a waiter's attempt therefore sits idle until that
waiter's next step, while the waiter fits it. Measured on the CarbonTeq
deployment with one host and six waiters: the host idled 877 s of a 1,274 s
test, in gaps of 5 min 7 s and 7 min 57 s, and 30 s of work took 21 minutes.

A pending run with `resubmission_attempt > 0` is now also ready when its latest
attempt ended `FAILED_TO_START_DUE_TO_NO_CAPACITY` and an instance in its
project has released a job since that attempt was **submitted** and still has
a free block (`busy_blocks == 0`, or `busy_blocks < total_blocks`), is `IDLE` or
`BUSY`, reachable and not deleted. The release time is the existing
`InstanceModel.last_job_processed_at`, which is written only when a job is
unassigned (`jobs_terminating.py`), so there is no new column and no migration.
The run pipeline already re-examines retrying runs every ten seconds, so a
release is noticed within one fetch interval.

**The wake is scoped to the market** (control plane ADR-049 decision 3,
dstack-facts §25d, 2026-09-23). A released instance wakes a waiter only if the
instance's offer matches the waiter's spot requirement — the test
`requirements_to_query_filter` applies to offers (`q.spot = req.spot`), done in
memory. The requirement is `JobSpec.requirements.spot` of the attempt's
no-capacity jobs; the market is `instance.resources.spot` of the instance's
stored offer (`get_instance_offer`). A spot waiter is woken only by a spot
instance, an on-demand waiter only by an on-demand one, an `auto` waiter by
either. Before this, a released on-demand instance woke every spot waiter too,
and each spent an attempt — on a cloud fleet, a placeholder — on capacity every
fit check refuses it. The other conditions stay in SQL; for a waiter with a
market requirement the query reads the latest 50 releases
(`_WAKE_CANDIDATE_LIMIT`, newest `last_job_processed_at` first) instead of one,
and an `auto` waiter still reads one row. It fails open: an instance with no
offer or one that does not parse counts as a release, and a job spec that does
not parse counts as `auto`.

The anchor is `JobModel.submitted_at`, not `last_processed_at`. An attempt can
find no capacity, the slot can free a second later, and the attempt's job is
only marked failed after that — measured, not hypothetical. Anchoring on the
failure misses exactly that release.

Beyond the market it is deliberately loose. Fit is not checked, so a wake can be wasted: at most
one attempt per waiter per release, because the woken attempt's own
`submitted_at` postdates the release. Every waiter in the project is woken, not
only the highest priority. Placement decides who gets the slot, which today is
a race (control plane S-29, ADR-046). Other retry events — `error`,
`interruption` — keep the full backoff: a crash loop is not a wait for a slot.

Touches `server/background/pipeline_tasks/runs/pending.py` (a new
`capacity_released_since_last_attempt`, a `PendingContext.capacity_released`
field and one condition in `process_pending_run`, plus the market helpers
`_spot_requirement` and `_released_into`) and two lines in
`runs/__init__.py`'s `_load_pending_context`. Coverage is `TestWakeOnRelease` in
`src/tests/_internal/server/background/pipeline_tasks/test_runs/test_pending.py`,
sixteen cases on SQLite and PostgreSQL: a release wakes the run, including one
that happened while the attempt was being failed, and a freed block on a
shared instance; a release before the attempt, an instance with no free block,
another project's instance, an unreachable or terminating instance, and a
non-capacity failure do not. The three positive cases fail against `1c03109`,
and all three fail if the anchor is changed to `last_processed_at`.

The market cases: `..._ignores_an_on_demand_release_for_a_spot_waiter` and
`..._ignores_a_spot_release_for_an_on_demand_waiter` fail against `101c806`
(the run is resubmitted); `..._wakes_a_waiter_in_the_same_market` (spot→spot,
on-demand→on-demand, `auto` woken by either), `..._finds_a_same_market_release_beside_another_market`
(three newer other-market releases ahead of one match; fails if the bound is
one row) and `..._fails_open_on_an_instance_without_an_offer` pass on both.

*Not covered.* A newly added instance that has never run a job has no
`last_job_processed_at` and wakes nobody; its waiters keep their timers. Nor
does a fleet imported from another project. Nor does any instance whose
provisioning data is not `dockerized` — RunPod, Vast.ai, Kubernetes, Slurm and
the sim backend: `jobs_terminating.py` moves it to `TERMINATING` in the same
update that stamps `last_job_processed_at`, so it is never `IDLE` or `BUSY`
with a release to report. More than 50 other-market releases since one attempt
began also fall back to the backoff.

*Rebase.* `process_pending_run`'s readiness condition and `_load_pending_context`
are small, but they sit in a hot pipeline beside the jittered backoff. Check that
`last_job_processed_at` is still written only on unassignment; if upstream starts
writing it on assignment, every waiter would wake on every placement. Check that
`Requirements.spot` is still the tri-state `requirements_to_query_filter` copies
onto the offer query, and that the offer still carries `instance.resources.spot`;
if placement stops honouring the spot requirement, drop the market scoping with
it. *Retire*
it if upstream wakes capacity waiters on release, or if the backoff stops
applying to `no-capacity` retries.

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

**"Unsupported" was true of tasks only, and it was degraded rather than blocked**
(D-20). `configurators/task.py` rejects `off` for tasks; dev environments and
services map it to `None`, so `StopDuration` reaches the runner as nil. This
delta then set `killDelay = 0` for that case — and in Go, `cmd.WaitDelay == 0`
means *no* delay is enforced, not an immediate one. Such a workload received
SIGINT and was never killed; recovery fell back to the 300-second `remove_at`
instead of upstream's ten seconds. Upstream had no nil case at all: it set ten
seconds unconditionally in `NewRunExecutor` and never reassigned it, and this
delta removed that line when it made the value per-job.

`SetJob` now floors a nil `StopDuration` at `defaultKillDelay` (ten seconds,
upstream's value). Zero is still zero and still means immediate: `stopImmediately()`
sends SIGKILL directly, so `WaitDelay` never applies on that path.

`executor_test.go` asserted `assert.Zero(t, ex.killDelay)` for the nil case,
which pinned the defect rather than the intent. It now asserts the floor.

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

**Retirement condition, which this entry lacked.** At 83 lines this is the
largest production hunk in the fork, and the only retirement clause that
mentioned the executor was `stop_duration`'s — so retiring on *that* condition
would have silently dropped this too, reintroducing the bug it fixes. They are
independent: a bounded stop duration is useless if the signal never reaches the
workload, and the signal reaching the workload is useful whatever the deadline
is.

Retire this delta only when upstream signals the **terminal's foreground
process group** rather than `cmd.Process` — check `startCommand()`'s cancel
function for a `TIOCGPGRP` lookup or an equivalent, not merely for a changed
signal or a new setting. If upstream instead stops running the workload under
`/bin/sh -i -c`, the premise disappears and the delta can go; verify by reading
`JobConfigurator._commands`, because a non-interactive shell does not enable job
control and the process group question does not arise.

`TestExecutor_MaxDuration` previously asserted the error text `killed`. That
encoded the defect: the workload ignored the graceful signal and survived until
the hard kill. It now asserts the actual contract, that the job is terminated
for exceeding its max duration.

Known bound: after `WaitDelay` elapses Go kills only the command, so a workload
that ignores the interrupt for the entire stop duration can outlive the runner's
own kill until the shim's container stop removes it. That is acceptable because
both deadlines derive from the same stop duration.

### Apply plugin policies on the deprecated submit route

Upstream calls `apply_plugin_policies()` from `runs.get_plan()` and
`runs.apply_plan()`, but not from `runs.submit_run()`. The deprecated
`POST /api/project/{project}/runs/submit` route calls `runs.submit_run()`
directly, so a request to it skips every server-side apply policy. The route is
gated only by `ProjectMember()`, which admits any project role, so any member's
token could submit a run that bypassed admission control entirely. That reduces
the CarbonTeq policy layer — compute windows, run-duration ceilings, the cloud
permission flag and priority bands — from enforced to advisory.

The candidate delta, all in
`src/dstack/_internal/server/routers/runs.py`:

- applies plugin policies to `body.run_spec` inside the `submit_run` route
  before delegating to `runs.submit_run()`; and
- re-parses the returned spec, matching how `get_plan()` and `apply_plan()`
  handle a policy's return value.

The call belongs in the route rather than in `runs.submit_run()` because
`apply_plan()` has already applied policies by the time it calls that function.
A second pass there would re-apply them to an already-modified spec, which for a
band-mapped `priority` would move the value again.

The route is kept rather than deleted. Deleting it would close the same hole,
but roughly twenty existing upstream tests exercise it, and carrying that diff
through a 4,000-line test file is more rebase surface than the nineteen lines
this costs.

Regression coverage is `TestSubmitRun::test_applies_plugin_policies_that_reject`
and `TestSubmitRun::test_persists_the_spec_a_plugin_policy_returns` in
`src/tests/_internal/server/routers/test_runs.py`. Both were confirmed to fail
with the delta reverted.

The design this supported is recorded in the control-plane repository as
`docs/00-context/ctpolicy-history.md`. The plugin itself lived in `policy/`,
which was additive and carried no rebase surface; both left this tree at T3 and
N2. The delta below is not affected — it closes a guardrail bypass that exists
whether or not any policy plugin is installed.

### One-shot deferred start

The compute-window hold (ADR-022). A run admitted at 03:00 for a team whose
window opens at 08:00 must sit inert and enter the queue at 08:00, with the
control plane holding nothing of its own.

dstack already has the holding and the releasing: `RunStatus.PENDING`,
`RunModel.next_triggered_at`, and a release predicate that picks up any run
whose `next_triggered_at` has passed. What it lacks is a way to say *hold until
this instant*. Its only trigger source is a cron expression, and cron means
recurring — the terminating pipeline recomputes the next fire time when an
execution ends, so a window hold expressed as a cron would quietly become a
daily job. That was checked against the source, not assumed.

The delta:

- adds `start_after`, an absolute UTC instant, to `ProfileParams` in
  `src/dstack/_internal/core/models/profiles.py`, directly after `schedule`;
- gives `_get_next_triggered_at()` in
  `src/dstack/_internal/server/services/runs/__init__.py` an `after_execution`
  keyword, returning the one-shot instant on the submission path and **None**
  on the post-execution path;
- holds the run at submission by extending the same file's `PENDING` condition
  to cover `start_after`; and
- passes `after_execution=True` from
  `src/dstack/_internal/server/background/pipeline_tasks/runs/terminating.py`.

A cron and a `start_after` on the same spec is not an error: the schedule wins,
and the precedence is asserted rather than left to be discovered.

No migration. `next_triggered_at` already exists and already holds an absolute
instant; this only widens what may compute one.

Regression coverage is `TestOneShotDeferredStart` in
`src/tests/_internal/server/services/runs/test_runs.py` — five cases, of which
the load-bearing one is that a one-shot returns no next trigger after it runs
while a cron still does — and
`test_creates_pending_run_if_run_has_a_one_shot_start` in
`src/tests/_internal/server/routers/test_runs.py`.

### Priority gate at placement

Control plane ADR-046 and S-29, 2026-09-16. Upstream reads `RunModel.priority`
in one place, the submitted-jobs fetch's `ORDER BY`, and on a fleet that is not
saturated it decides almost nothing. A new job is fetched alone. A shared batch
is placed by concurrent workers racing for the instance lock. A run waiting for
capacity retries on its own jittered timer. Measured on the CarbonTeq
deployment, a priority-29 run took a host that a waiting priority-99 run
fitted: on an idle host by arriving first, after a release by its timer firing
first, and from a same-second batch in priority order, because the 99s were
attempted first against a still-busy host.

In `_select_assignment`, once `find_optimal_fleet_with_offers` has returned
existing-instance offers, the job **yields** if a waiting run of strictly higher
priority fits one of those instances. Yielding returns the pipeline's existing
`_DeferSubmittedJobResult`. The job is not failed, uses no retry attempt, and is
fetched again after `min_processing_interval`.

- **Waiting** means one of two things. The run is `SUBMITTED` with its master job
  `SUBMITTED` and not yet `instance_assigned`. Or it is `PENDING` with
  `resubmission_attempt > 0` and its latest master job ended
  `FAILED_TO_START_DUE_TO_NO_CAPACITY`. Waiters are taken from the job's project,
  highest priority first, 50 fetched and at most 10 fit-checked.
- **Held runs never count.** A run held for a window or a schedule is `PENDING`
  with `resubmission_attempt == 0`, so a closed window cannot block the fleet.
- **Fits** is dstack's own `get_instance_offers_from_instances` for the waiter's
  stored job spec against the offered instances. The waiter's `fleet_id` and
  `fleets` pins are checked first. A multinode waiter never fits a single slot.
- **Bounded** by the yielding run's no-capacity retry window, anchored where
  `_should_retry_job` anchors a first start (`submitted_at`, or
  `next_triggered_at`). A run with no `no-capacity` retry is not gated at all,
  since nothing would end its yield.
- **Equal priority never yields.** Order within a priority stays as upstream has it.
- **It fails open.** Any exception is logged and the job places normally.
- **Not gated:** jobs with `job_num > 0` or of a multinode run, whose cluster is
  already forming; targeted `instances` placement; and new capacity, which is a
  market rather than a slot.

This is not strict head-of-line priority. A higher run that fits none of the
offered instances blocks nobody.

**It depends on "Wake a run waiting for capacity when an instance frees a
block".** Without that delta, a lower run that yields to a higher run sleeping
between backoff steps leaves the slot idle for up to ten minutes. Release the two
together.

The logic is an additive module,
`server/background/pipeline_tasks/priority_gate.py`. The upstream file changes
by one import and one call in `_select_assignment` in `jobs_submitted.py`.
Coverage is `TestPriorityGate` in
`src/tests/_internal/server/background/pipeline_tasks/test_submitted_jobs.py`,
nine cases on SQLite and PostgreSQL:

- **Yields** to a submitted higher waiter that fits, and to a higher run
  retrying for capacity.
- **Places normally** when the higher run fits nothing (control plane trap 8),
  for a held higher run, at equal priority, past the retry window, without a
  no-capacity retry, and when the gate raises.
- **A batch processed low first** yields the low job and places the high one.

The three yield cases fail with the gate disabled. The whole
`pipeline_tasks/` suite gives 470 passed with 460 PostgreSQL skips, and the
submitted-jobs file fails nothing before or after.

*Not covered.* Volumes are not part of the waiter's fit, so a waiter whose volume
cannot attach to the instance still counts, bounded by the yielding run's window.
Fleets imported from another project are not searched for waiters.

*Rebase.* Re-read `_select_assignment` and `find_optimal_fleet_with_offers`'s
return shape. The gate needs existing-instance offers before any instance is
locked. If upstream moves instance choice under the lock, the call has to move
with it. Re-check `get_instance_offers_from_instances`' signature and the
`instance_assigned` meaning. *Retire* it if upstream placement becomes
priority-aware across concurrent workers and pending retries.

### The sim backend

Control plane ADR-048 (proposed; accepted by its task `G1` on spike `SB1`),
2026-09-23. The control plane needs dstack's own placement, retry and
interruption code to run with no cloud, so the same scenarios can run on a
simulator and then on real hardware. Upstream has no local or mock backend,
and its server reaches every runner through an OpenSSH tunnel with no bypass.
So the simulator is a backend here, and its hosts are containers running a real
sshd in front of a fake runner.

`sim` is a container backend shaped exactly like RunPod. `run_job` asks an
external **sim controller** for a host and returns provisioning data with no
hostname; `update_provisioning_data` fills the address in once the controller
reports the host running, and raises `ProvisioningError` if it failed or
vanished; `is_instance_present` is whether the controller still knows the host,
so a spot interruption is confirmed at once, as this fork already does for
RunPod; `terminate_instance` deletes it and treats a 404 as done. `get_offers`
reads the controller's catalogue on every call, with no offer cache, because
stock and faults change between scenarios. It converts each entry through
gpuhunt's `CatalogItem` and `catalog_item_to_offer` and filters with
`filter_offers_by_requirements`, so the market, GPU, CPU and price filter is the
one every gpuhunt backend uses. The sim has no filter of its own. A controller
`409` from `POST /hosts` is `NoCapacityError`.

The controller, its catalogue, the host image and every fault live in the
control-plane repository (`sim/`), not here. This package is deliberately a thin
HTTP client (`core/backends/sim/client.py`, whose docstring is the wire format),
so the simulator can grow without new fork deltas. It parses the controller's
responses with `__response__`, which ignores unknown fields, because a strict
parse would turn every field the controller adds into a failed run.

**It never loads by accident.** `core/backends/sim/configurator.py` raises
`ImportError` unless `DSTACK_SIM_ENABLED=1`, and `configurators.py` imports every
configurator inside `try/except ImportError`, so on any other server the
backend is simply unavailable. A config naming `type: sim` still parses,
because the models are plain data, and is then refused as a backend the server
does not have. The control plane's `harness/checks/sim-isolation.sh` fails if
either Dokploy compose names the variable or the sim image.

The delta:

- adds the package `src/dstack/_internal/core/backends/sim/` (`models.py`,
  `client.py`, `compute.py`, `backend.py`, `configurator.py`);
- adds `SIM = "sim"` to `BackendType` in
  `src/dstack/_internal/core/models/backends/base.py`;
- adds one `try`-import block to `src/dstack/_internal/core/backends/configurators.py`;
- adds `SimBackendConfig` / `SimBackendConfigWithCreds` to the three config unions in
  `src/dstack/_internal/core/backends/models.py`.

No migration: `BackendType` has been stored as a string since
`bc8ca4a505c6_store_backendtype_as_string`. The config has no credentials, only
`controller_url` and an optional `provisioning_timeout_seconds`; the default is
the server's generic ten minutes.

Coverage is `src/tests/_internal/core/backends/sim/test_compute.py`, 19 cases:
offer conversion, the market filter, availability, no caching, `409` as no
capacity, the provisioning-data lifecycle, presence and idempotent termination.
It also asserts that only `DSTACK_SIM_ENABLED=1` registers the backend (unset,
`0` and `true` do not). End to end, the control plane's `sim/smoke.sh` takes a
task from `submitted` to `done` through the unchanged SSH tunnel and runner
protocol, with `run.cost` equal to the catalogue price times the duration.

*Rebase.* Additive, so the surface is small. Re-check the three upstream edits,
the abstract `Compute` surface (`get_offers`, `run_job`, `terminate_instance`,
and this fork's `is_instance_present`), `catalog_item_to_offer`'s signature, and
`JobProvisioningData`'s fields. *Retire* it when the control plane stops needing
a simulator (ADR-048's reversal): delete the package and the three edits.

## Fixed: version.sh described the wrong repository

`version.sh` exists so a release version cannot be forgotten. Both of its
documented invocations were broken (defect D-7):

- from inside a submodule checkout it exited 1 — `usable_git_repo` probed for
  `.git/objects`, and a submodule's `.git` is a *file* holding a `gitdir:`
  pointer, so a perfectly usable checkout was called unusable and the raw reader
  then failed on the same file;
- from a consumer repository that vendors this one, it returned the CONSUMER's
  HEAD with exit 0 — tagging an image with a foreign commit, silently.

The second is the dangerous one: the version looks well-formed and pins nothing,
and a version that does not move is exactly how workers silently keep old
binaries.

It now resolves against its own location rather than the caller's cwd, probes
with `git rev-parse --git-dir` so a pointer file works, and asserts the resolved
commit's tree actually contains `src/dstack/version.py` — if it does not, this
is somebody else's history and the script fails rather than guessing.

Root discovery tries three candidates because two layouts must work: a normal
checkout, where the script is three levels below the root, and the image builds,
which COPY it alone into a flat WORKDIR beside a trimmed `.git`. Verified in
both, including a real `--target version` build.

## Fixed: the compute-window hold never dispatched

The deferred start shipped and never worked (defect D-1). A spec carrying
`start_after` killed the apply *before* the guardrail was reached:

```
TypeError: Object of type datetime is not JSON serializable
```

`get_profile_excludes` does not drop `start_after`, so it reached
`rest_plugin`, which serialised with pydantic-v1 `.dict()` — keeping the
datetime — and handed it to `requests.post(json=…)`, which encodes with the
stdlib. Nothing caught it: `_call_plugin_service` catches `ConnectionError` and
`RequestException`, `apply_plugin_policies` catches `ValueError`, and a
`TypeError` is neither.

Two halves, because there are two serialisation problems in one call:

- the payload is now encoded with `pydantic_encoder`, which handles datetimes;
- it still goes through `.dict()` rather than `.json()`, because the override in
  `_models.py` pops `__orig_class__` and pydantic v1's `.json()` bypasses
  `dict()` via `_iter()` — using it reintroduces the
  `_GenericAlias is not JSON serializable` bug that override exists to prevent.
  That was verified by breaking the suite with it.

`start_after` also gains a validator normalising to aware UTC.
`next_triggered_at` is a `NaiveDateTime` column that strips tzinfo going in and
re-attaches UTC coming out, so a naive local time would have been stored as
though it were UTC and the run would fire at the wrong hour, silently, and only
for submitters outside UTC.

The regression test is
`test_on_apply_posts_an_encodable_payload_with_a_deferred_start`, and it asserts
the payload is *encodable* rather than that the call happened. That distinction
is the point: every other test in that file mocks `requests.post` and never
inspects what it was given, which is why `_models.py` already carried a note
that this class of failure "doesn't happen though when running the code in
pytest, only when running the server". Confirmed it fails against the old
serialisation before being trusted.

## Compatibility and release

Build the server, runner, and shim from the same fork commit and give the
runner/shim one matching component version. A mixed rollout is unsafe:

- a patched server with an old runner still gives the process ten seconds;
- a patched runner with an old server receives no bounded field; and
- a new component version is required for dstack worker reconciliation to
  install the binaries.

The MVP deployment enforces this by construction rather than by convention: both
images derive their version from the commit in the build context, and the
version the server reports is the one baked into its wheel, so the component set
cannot silently disagree and no operator has to remember to bump anything. See
`app/deploy/dstack-server/README.md` in the control-plane repository.

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

Re-run for the deferred start (2026-08-31), after `policy/` was deleted:

```text
3,099 Python tests passed, 1,213 skipped   (src/tests, proxy suite excluded:
                                            it needs `openai`, unrelated)
ruff 0.12.7 check and format clean         (the version pyproject pins; a
                                            newer ruff reformats unrelated files)
git diff --check passed
```

The Go packages were not re-run: this delta is Python-only and does not touch
the runner payload.

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

The replacement-capacity-wait successor (C-67) passes all 29 SQLite tests in
`test_runs/test_active.py`, with 29 PostgreSQL variants skipped. Three of its five
new tests fail against `30472de`. The whole `background/pipeline_tasks` test
directory has 461 passes, 451 PostgreSQL skips and no failure on SQLite. Ruff
check and format are clean. No spot reclaim, production run or provider canary
has exercised it.

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

### Digest-pinned build bases

`Dockerfile` pins its upstream base by digest (`BASE_IMAGE`, line 13).
`Dockerfile.binaries` did not: it floated on `golang:1.25-bookworm` and
`nginx:alpine`, and it is the build that produces the runner and shim **every
worker downloads and executes**, with `CGO_ENABLED=1` for the shim — so the
toolchain and the C library behind it are part of the artifact. A moved tag
would change what lands on the fleet with every pin in the consumer repository
unchanged. Both are digest-pinned now.

Bumping either is a deliberate edit here plus a rebuild and republish of the
binaries image, exactly as for the server base.

### The root `.dockerignore`

Not a behaviour change, and recorded here because it changes the context of
**every** docker build from this tree and nothing else documents it.

Without it a build ships the whole working tree to the daemon — over a gigabyte
once a local `.venv` exists, against roughly 50 MB of source. The exception that
matters is the git ref metadata: `docker/server/carbonteq/version.sh` resolves
HEAD from `.git/HEAD`, `.git/refs` and `.git/packed-refs` to derive the release
version without a git binary, so those are re-included after `.git` is excluded.

**Rebase surface: none** — upstream has no root `.dockerignore`, so there is
nothing to conflict with. **Retirement: never, while the carbonteq Dockerfiles
build from this tree.** Deleting it does not fail a build; it makes every build
slow, and dropping the three `!.git/…` re-includes breaks `version.sh` with an
empty version rather than an error.

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

After merging current `carbonteq/master`, the release-focused Python matrix
passed 271 tests with 130 PostgreSQL variants skipped. Five selected
managed-storage/retry submitted-job tests passed with five PostgreSQL variants
skipped; the broader submitted-job file still has exactly the same eight known
SQLite multinode/placement failures and no new failure. Ruff and
`git diff --check` pass. Go is not installed in this release workstation, so
the runner executor/schema packages remain a required CI gate rather than a
locally re-claimed result.

## Rebase and retirement

Rebase from the exact upstream tag or commit, then inspect the runner payload
schema, task configurator, terminating pipeline, Go job schema, and executor
cancel path as one conflict-sensitive unit. Run all tests above plus the live
cancellation gate. Retire the fork only after an upstream release propagates
the same bounded value through both server and runner and passes the CarbonTeq
qualification unchanged.

The submit-route delta is a separate unit. Inspect the runs router alongside
`runs.submit_run()` and `runs.apply_plan()`: the delta is only needed while
`submit_run()` itself does not apply policies, and only correct while
`apply_plan()` still does. Retire it if upstream either removes the deprecated
route or moves the policy call into `submit_run()` — and in the latter case
remove the route-level call, or policies will be applied twice.

The deferred start is a third unit, and the cheapest of the three to get
subtly wrong. Inspect `_get_next_triggered_at()` together with **both** its call
sites — submission in `services/runs/__init__.py` and post-execution in
`background/pipeline_tasks/runs/terminating.py` — as one conflict-sensitive
group.

The failure to guard against is a held run becoming recurring: it fires once per
window forever, nothing breaks loudly, and the first symptom is a duplicated
workload rather than an error.

**This note used to name the wrong line** (D-21). It said the hazard was losing
`after_execution=True` at the terminating call site. That kwarg is passed only
where it cannot be read — the call sits inside
`if run_spec.merged_profile.schedule is not None`, and `after_execution` is
consulted only in `_get_next_triggered_at()`'s `schedule is None` branch. For a
`start_after`-only run the call never happens. Deleting the kwarg changes
nothing.

**The line that actually protects this is upstream's**
`if run_spec.merged_profile.schedule is not None` in `_get_run_update_map()`
(`background/pipeline_tasks/runs/terminating.py`) — a line the fork does not
touch, which is precisely why a rebase can widen it without anyone noticing.
The plausible widening is `schedule is not None or start_after is not None`.

`TestOneShotDeferredStart` does not cover this. It calls
`_get_next_triggered_at()` directly and **stays green through that exact
change** — verified by making it. The test that catches it is
`test_a_one_shot_deferred_start_terminates_rather_than_re_pending` in
`src/tests/_internal/server/background/pipeline_tasks/test_runs/test_termination.py`,
which goes through the pipeline. Keep both: one pins the function's contract,
the other pins that production reaches it.

Adding a field to `ProfileParams` also carries a test tax that is easy to
misread as breakage: several suites compare a whole serialized profile against a
literal dict. After any change there, `grep -rn '"schedule": None' src/tests` and
add the new key beside it — currently four sites in
`routers/test_runs.py` and three in `routers/test_fleets.py`.

Retire the deferred start if upstream gains a one-shot start time of its own, or
if compute windows stop being a requirement.

The sim backend is a fourth unit, and additive: its rebase and retirement
conditions are in its own section above.

Because the plugin hook is upstream-experimental, also re-check
`ApplyPolicy.on_run_apply`'s signature on every rebase.

The packaged `d2586c3871525e461bcbc442deaa511af2a87758` candidate additionally
passed a real RunPod Secure Cloud spot canary on an RTX PRO 6000 Blackwell
Server Edition. dstack observed submission through `done`, CUDA reported
97,887 MiB visible VRAM, and fleet deletion left the RunPod account with zero
active Pods.

Current deployed fork commit:
`85cab941fb4f8e243c7014c278ea01705f89651e`.

## Merging `master` into `dstack-cp-mvp` (2026-09-07)

`master` carries the qualified fork releases — the RunPod backend work, the
retry budget, managed storage rotation, gateway `replicas`, and `full_offers`.
`dstack-cp-mvp` carries the deployment: the Dokploy stack, the policy and quota
layer, the submit-route policy call, and the one-shot deferred start. Master is
merged into the MVP, never the reverse; the reverse would drag deployment
configuration into the release line.

The two sides overlap in seven files, and every overlap is in a disjoint region
of the file:

- `profiles.py` — `start_after` on `ProfileParams` against `duration_by_event`
  and `max_attempts_by_event` on `ProfileRetry`. Different classes.
- `routers/runs.py` — the submit-route policy call against `full_offers` on the
  plan route. Different routes.
- `services/runs/__init__.py` — `submit_run` and `_get_next_triggered_at`
  against `get_plan`. Different functions.
- `executor.go` — the `SetJob` kill-delay floor against env logging in
  `execJob`. Different functions.
- Two test files and this ledger.

**The hazard this section exists to record:** master does *not* touch
`background/pipeline_tasks/runs/terminating.py`, so upstream's
`if run_spec.merged_profile.schedule is not None` in `_get_run_update_map()` —
the line that keeps a `start_after` run one-shot, as the deferred-start section
above explains — survives the merge unwidened. Check it again on the next merge.
That is the one line whose loss would be silent.

Alembic resolves to a single head after the merge (`8d9e0f1a2b3c`): master's six
migrations include their own merge-heads revision and the MVP adds none. The
deployment needs a migration on the next deploy, not a hot swap.

Only this file conflicted, in three places, all of them two sides appending
ledger prose. Both sides were kept. Master's deployed-commit trailer replaced
the older `Published fork commit:` branch trailer.

**Known state at merge time:** seven `routers/test_runs.py` tests fail — the
four `TestSubmitRun` cases, both `TestListRuns` cases, and
`TestApplyPlan::test_submits_new_run_if_no_current_resource`. They fail on
`master` alone and pass on `dstack-cp-mvp` alone, so the merge inherits them
rather than causing them: master added `image_readiness` to the job submission
response without adding the key to those literal expected dicts. This is the
same test tax the deferred-start section describes, from the other direction.
Fix it on `master` and merge the fix down; do not patch it here.
