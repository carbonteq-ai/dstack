# CarbonTeq dstack fork ledger

## Status

Published candidate branch. The working tree is based on upstream dstack
`0.20.29` at commit `2f9618f4d521140350efd1b344412d122c1e0322`.
`origin` points to `carbonteq-ai/dstack` and `upstream` points to
`dstackai/dstack`. Consumers may pin only the published CarbonTeq branch head
by full SHA; deployment remains blocked until one matching server/runner/shim
release is built and qualified.

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

Before publication, repeat the Python suite with PostgreSQL enabled, build the
server and both binaries from the immutable candidate commit, and run a live
Docker cancellation whose handler takes more than ten seconds but less than
the configured stop duration. The release must prove the finalizer marker,
Trackio terminal state, container removal, and worker-idle state.

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

The failure to guard against is losing `after_execution=True` at the terminating
call site. Nothing breaks loudly: every held run simply becomes recurring, firing
once per window forever, and the first symptom is a duplicated workload rather
than an error. A rebase that takes upstream's version of that line reintroduces
it silently, which is why `TestOneShotDeferredStart` asserts the negative case.

Adding a field to `ProfileParams` also carries a test tax that is easy to
misread as breakage: several suites compare a whole serialized profile against a
literal dict. After any change there, `grep -rn '"schedule": None' src/tests` and
add the new key beside it — currently four sites in
`routers/test_runs.py` and three in `routers/test_fleets.py`.

Retire the deferred start if upstream gains a one-shot start time of its own, or
if compute windows stop being a requirement.

Because the plugin hook is upstream-experimental, also re-check
`ApplyPolicy.on_run_apply`'s signature on every rebase.

Published fork commit: branch `codex/graceful-cancellation-stop-duration`
(record the exact head SHA in the consumer repository after publication).
