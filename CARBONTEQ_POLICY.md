# CarbonTeq dstack policy layer

## Status

Phase 1, on branch `dstack-cp-mvp`. Compute windows, run-duration ceilings, the
cloud permission flag, two-level priority and per-user overrides are enforced.
Budgets, the usage snapshot, the background enforcer and the `dstack-quota`
companion CLI are planned but not built; see [Build order](#build-order).

This document is the decision record. [`CARBONTEQ_FORK.md`](CARBONTEQ_FORK.md)
remains the rebase ledger for changes inside upstream files;
[`policy/README.md`](policy/README.md) is the package's own reference; and
[`docker/server/carbonteq/README.md`](docker/server/carbonteq/README.md) is the
deployment guide.

## The problem

We need teams and users with quotas and policies on top of dstack: fixed time
windows, time budgets, dollar budgets that apply only to cloud, a per-team
"may use NeoCloud" flag, two-level static priority, user-level overrides,
rejection at submission, queue ordering that never preempts running work,
termination only for window/timeout/budget breaches, and absolute admin control.

Constraints: policy in configuration files rather than a database; every
decision computed at runtime; no duplication of concepts dstack already models;
SSH fleets only, shared by all teams; users keep the official dstack CLI; and
the fork stays as small as possible, because every line of it is rebased against
upstream.

## What the codebase actually does

These were traced in this tree before any design work. The corrections are the
load-bearing part — one of them changed the whole architecture.

### The finding that changed the design

**A shared SSH fleet does not force all teams into one project.** dstack has a
first-class SSH-fleet **export/import** feature: `ExportModel` (with
`is_global`), `ImportModel` and `ExportedFleetModel` in `server/models.py`,
serviced by `services/exports.py` and driven by the `dstack export` /
`dstack import` CLI commands.

- `services/exports.py` refuses to export a cloud fleet — *"Can only export SSH
  fleets"* — so the feature exists precisely for our shape.
- A global export is auto-imported into every project, and **new projects
  auto-import all global exports** (`services/projects.py`).
- Run planning genuinely uses imported instances: `get_pool_instances()` and
  `select_instances_by_selectors()` in `services/instances.py` both match
  `or_(own project, imported)`, and `services/runs/plan.py` calls the former.

The global host-uniqueness check in `list_active_remote_instances()` is
consistent with this: a host belongs to exactly one fleet, but that fleet can be
shared with many projects. Isolation therefore needs **no fork at all** — not a
patched `list_runs`, and not a WebSocket-proxying API gateway.

### The finding that produced our only fork line

**`apply_plugin_policies()` is called from `get_plan()` and `apply_plan()`, not
from `submit_run()`.** The deprecated `POST /api/project/{p}/runs/submit` route
calls `runs.submit_run()` directly and so bypassed every apply policy, gated
only by `ProjectMember()`. Any project member's token could have used it to skip
admission control entirely. See [Fork surface](#fork-surface).

### Other verified facts the design rests on

- **Priority never preempts running work.** `RunModel.priority` is read in
  exactly one place server-wide: an `ORDER BY priority DESC, last_processed_at
  ASC` in `jobs_submitted.py`, over a fetch filtered to `JobStatus.SUBMITTED`.
  The running-job pipeline does not order by priority, and no preemption logic
  exists anywhere. Requirement 8 is satisfied natively; we add nothing.
- **That ordering is global across projects** — the fetch has no project filter
  — which is what makes priority bands work across team projects.
- **Priority is changeable after submission**, via `_CONF_UPDATABLE_FIELDS =
  ["priority"]` in `services/runs/spec.py`. That path goes through
  `apply_plan()` → `apply_plugin_policies()`, so a user cannot re-apply their
  way out of their band.
- **`max_duration` is enforced by the runner, not the server.** The Go executor
  runs the timer in the VM; the server only reads the resulting state back.
  There is no server-side clock check for it. So the bound holds on the SSH
  fleet and even if the server is unreachable, and it composes with this fork's
  graceful-cancellation delta.
- **SSH instances are hard-coded `price = 0`**, and cost is computed as
  `price × duration`. A dollar budget therefore excludes on-prem automatically —
  requirement 3 falls out of dstack's own cost model with no special-casing.
- **Cost is never persisted**; it is computed at read time, and `JobModel` has
  no `finished_at` column (it is derived from `last_processed_at`). There is no
  usage aggregation anywhere in dstack.
- **A plain `ProjectRole.USER` can stop or delete any run in their project** —
  `stop_runs`/`delete_runs` filter only on project and run name, with no owner
  check. Per-team projects make this harmless.
- **Global admins bypass every project-level permission check**, so requirement
  10 is already true in dstack.
- **Usernames and project names are both immutable.** `update_user()` takes the
  username as its lookup key, and `update_project()` changes only `is_public`
  and `templates_repo`. Both are therefore safe as configuration keys — and
  renaming `main` is not an option.
- **`server/config.yml` declaratively creates projects and reconciles their
  backends** at boot, and sets `default_permissions`. It is applied once at
  startup and never re-read.
- **There is no plugin hook for background tasks or HTTP routes**, and no
  third-party CLI subcommand mechanism. The planned enforcer and companion CLI
  must therefore be separate processes.
- The hook receives `user: str` and `project: str` — **names, not models** — with
  no DB session, running synchronously on a shared 128-thread executor while the
  request still holds a DB session.
- A `ValueError` raised in a policy becomes a `ServerClientError`, which is a
  `ClientError`, which the CLI prints verbatim. Rejection text reaches the user
  unaltered.

## Decisions

### 1. One dstack project per team

`main` keeps the SSH fleet and exports it globally; each team is its own dstack
project. Team membership is dstack's `MemberModel` — we invent no parallel
entity — and the plugin's `project` argument *is* the team identity.

This makes isolation native: run listing is already project-scoped, so a
`team-research` member's `dstack ps` shows only their team's runs, and the
missing owner check on `stop_runs` stops mattering because they cannot name
another team's runs.

*Rejected:* a single shared project keyed on username. It matches the original
assumption but leaves every user able to see and stop every other team's runs,
which would have required either a core patch or a proxy that also had to relay
WebSockets for `dstack logs` and `dstack attach`.

*Consequence for the live deployment:* project names are immutable and fleets
cannot move between projects, so `main` stays the fleet owner rather than being
renamed to `infra`. The enrolled worker, the binaries rollout and the
cancellation smoke test are untouched.

### 2. Accept dstack's best-effort queue ordering

Priority bands, no fork. A higher-priority team is always *attempted* first, but
a high-priority run that cannot be placed does not block a lower-priority run
that fits — upstream documents this, and the ordering is over a fetch batch of
the top ~80 submitted jobs.

*Rejected:* forking `JobSubmittedPipeline` for strict head-of-line blocking.
It would satisfy requirement 5 literally at the cost of conflict-prone fork
surface in a hot pipeline and an idle fleet whenever the top job cannot fit.

**This is the one requirement not met as literally written.** See
[Risks](#risks-and-known-gaps).

### 3. An in-process Python plugin

`ctpolicy` is installed into the server image we already build, and enabled by
`plugins: [ctpolicy]` in `server/config.yml`.

*Rejected:* the builtin `rest_plugin` with an external policy service. It adds a
network hop and a hard 8-second blocking timeout per apply, and it still only
receives `{user, project, spec}` — no roles — so it solves nothing we needed.

### 4. Deny users with no policy entry

A user who is a member of a team project but absent from that team's `users` map
is rejected, with a message naming the team and the file. `alice: {}` is the
terse way to grant the team defaults.

*Rejected:* an inherited `default_policy` block. Friendlier onboarding, but a
forgotten entry would silently grant compute — the failure mode a quota layer
exists to prevent.

The same principle is applied one level up: a project that is neither a
configured team nor listed in `ungoverned_projects` is rejected, so creating a
project cannot quietly create an ungoverned team.

### 5. Cloud settings merge key by key

Everything else in a user override replaces the team value outright, which is
what "user-level policies override team-level ones" means. `cloud` is the
exception: replacing the block wholesale would mean `cloud: {allowed: false}` on
a user silently discarded the team's `max_price` and `backends`, which widens
rather than narrows.

### 6. No `regions` restriction

Deliberately omitted. Regions would have to be applied through the same spec
field the shared on-prem fleet is matched by, so a cloud-oriented region list
could quietly make a team unable to run on-prem at all. Cost is already bounded
by `max_price` and, once built, the budgets.

### 7. No admin bypass list in the policy file

Listing global admins in `policy.yaml` would duplicate `GlobalRole`, which the
constraints forbid. Admins already have absolute control through dstack's own
role model and through owning this file; when an admin submits a run *to a team
project*, they are subject to that team's policy, which is the correct
behaviour. Admin work belongs in an ungoverned project.

## Architecture

```
 docker/server/carbonteq/config.yml   ──►  projects, backends,
   (bind-mounted, read-only)               default_permissions, plugins: [ctpolicy]
                     │                     applied once at boot
 ┌───────────────────┴──────────────────────────────────────────┐
 │  server  (upstream digest + dstack wheel + ctpolicy wheel)   │
 │                                                              │
 │  apply_plan()  ──►  apply_plugin_policies()  ──►  ctpolicy   │
 │  submit_run route ─┘        (fork delta)                     │
 │                                                              │
 │  JobSubmittedPipeline: ORDER BY priority DESC                │
 │  (global across ALL projects, SUBMITTED jobs only)           │
 └───────────────────▲──────────────────────────────────────────┘
                     │ re-read on mtime change
   policy.yaml ──────┘   (bind-mounted, hot-reloaded)
```

Enforcement lives in three places, only one of which is code we wrote:

| # | Requirement | Enforced by | Status |
| --- | --- | --- | --- |
| 1 | Fixed time windows | plugin rejects when shut; clamps `max_duration` to window close | done |
| 2 | Max time budget | plugin, against the usage snapshot | phase 2 |
| 3 | Dollar budget, cloud only | free — SSH is `price = 0`; plus the `max_price` clamp | phase 2 |
| 4 | NeoCloud boolean | **structural** (no cloud backend in the project, and backends are admin-only) **plus** the plugin pinning `backends` and rejecting cloud fleets/volumes/gateways | done |
| 5 | Two-level priority | plugin rewrites `priority` into the team's band | done (best-effort, see risks) |
| 6 | User overrides team | deep merge of `users.<name>` over `defaults` | done |
| 7 | Reject at submission | `ValueError` → `ServerClientError` → CLI text | done |
| 8 | Order queued only, never preempt | **native dstack**; we add nothing | done |
| 9 | Terminate only on window/timeout/budget | the **runner's** `max_duration`; enforcer as backstop | partial |
| 10 | Admin absolute control | **native** (`GlobalRole.ADMIN`) plus `default_permissions` | done |

### Why a snapshot file, not a DB session

Phase 2 needs usage data, and the hook cannot get it directly. It runs on a
worker thread of the shared 128-thread executor *while the FastAPI request still
holds a DB session*; the engine pool is 20 + 20 overflow, and dstack's own
`db.py` documents that cross-thread access requires a whole new engine rather
than the shared pool. A loopback HTTP call has the same problem plus a second
connection per apply.

So the enforcer will recompute usage from dstack's own database on its own
schedule and write a small JSON snapshot that the plugin reads. The snapshot is
a **cache, never authority** — nothing we own is load-bearing for a decision —
and staleness beyond a configured bound fails closed.

### Budget soundness (phase 2)

Clamping is sound only if admission subtracts *committed* cost as well as spent:

```
remaining = limit − spent − Σ(worst-case remaining cost of active runs)
clamp max_price and max_duration so that max_price × max_duration/3600 ≤ remaining
```

Without the committed term, N concurrently-admitted runs each individually under
budget can collectively exceed it. With it, the bound holds across concurrency
*and is enforced by the runner*, which is stronger than any server-side check.
The residue the enforcer must cover: window boundaries (the runner's clock
excludes provisioning), mid-run policy edits, and price changes on retry.

## Fork surface

One upstream file changed, plus its regression coverage. Recorded in
[`CARBONTEQ_FORK.md`](CARBONTEQ_FORK.md), which is the rebase contract.

`src/dstack/_internal/server/routers/runs.py` — the deprecated `POST /submit`
route now applies plugin policies before calling `runs.submit_run()`.

**Why this and not deletion.** The plan called for deleting the route. Deleting
it would have broken roughly twenty existing upstream tests in a 4,000-line test
file, and carrying that diff is worse fork surface than the nineteen lines the
fix actually costs. Applying the policies keeps every upstream test green and
closes the hole just as completely.

**Why in the route and not in `submit_run()`.** `apply_plan()` has already
applied policies by the time it calls `submit_run()`; a second pass there would
re-apply them to an already-modified spec, which for the priority band mapping
would move the value again.

Everything else is either additive (`policy/`, which upstream has no path for)
or in files we already own (`docker/server/carbonteq/*`).

**Not forked, deliberately:** team-scoped run filtering and stop-ownership
checks (unnecessary — per-team projects give them natively); strict priority
head-of-line blocking (decision 2); a run-level `termination_reason_message`
(accepted UX gap); a background-task hook (the enforcer will be its own process).

## Build order

- **Phase 0 — restructure dstack. No code.** `config.yml` with `main` plus the
  team projects and locked-down `default_permissions`;
  `dstack export create shared-hw --fleet mvp-workers --global`; move engineers
  to team-project memberships. Delivers isolation on its own.
- **Phase 1 — the plugin, stateless rules, and the fork delta.** *(this build)*
  Config loading with hot reload, windows, the duration ceiling, the cloud flag,
  priority bands, per-user overrides, and the fleet/volume/gateway guards.
  Delivers requirements 1, 4, 5, 6, 7, 8, 10.
- **Phase 2 — usage snapshot and budgets.** The snapshot writer, the oracle with
  staleness handling, and the budget checks with the committed-cost term.
  Delivers requirements 2 and 3.
- **Phase 3 — `dstack-quota`.** A read-only companion CLI over the same config
  and snapshot, so users can see their position before anything terminates.
- **Phase 4 — the enforcer backstop.** Terminates runs breaching a window
  boundary, an edited policy, or a budget the clamp could not bound, via
  `stop_runs(abort=False)` so `stop_duration` is honoured. Completes
  requirement 9.

Each phase is independently deployable.

## Risks and known gaps

**Priority is best-effort, not strict** (accepted, decision 2). Requirement 5 as
literally written — "any job from a higher-priority team *must* be scheduled
before any job from a lower-priority team" — is not satisfiable without forking
the submitted-jobs pipeline.

**Band ceiling.** Teams × per-team granularity must fit in 101 values. Three
teams leaves ~30 job-priority levels each; ten leaves ~10; past roughly fifty
teams the scheme stops being meaningful.

**Window overrun.** The runner's `max_duration` clock starts when the job starts
*running*, so a run clamped to the time left in a window can outlive that window
by roughly its provisioning time. Phase 4 covers the residue; until then this is
a real hole.

**On-prem cost is $0 by construction.** That is exactly requirement 3, but it
means the time budget will be the only lever on the SSH fleet. Worth confirming
that is intended rather than incidental.

**Usage accuracy (phase 2).** `finished_at` is derived from
`last_processed_at`, so figures are approximate to within one pipeline cycle,
and `price` must be parsed out of a JSON text column rather than read from an
indexed one. Fleet *idle* time is attributed to nobody — `InstanceModel` has no
`user_id` — so charging back idle on-prem capacity would need a different
mechanism entirely.

**Preemption reason is invisible in `dstack ps`.** There is no run-level
`termination_reason_message` column, and the run termination reason for a
server-initiated stop maps to no user-visible error string. Mitigated by leaning
on the runner's `max_duration` for the common case, which surfaces natively as
"max duration exceeded", and later by `dstack-quota`.

**Plugins are experimental upstream** — *"Backward compatibility is not
guaranteed across releases"*. The `ApplyPolicy` hook signature is the main
rebase exposure; `policy/src/ctpolicy/_compat.py` confines the `_internal`
imports so a break surfaces in one place.

**Executor pressure.** `on_run_apply` occupies one of 128 shared executor
threads while the request holds a DB session. It must stay pure CPU plus one
cached file read; blocking I/O added later will bite under load.

**Layered bind mount.** `config.yml` is mounted inside the `server-data` volume's
mount point, which relies on Docker ordering mounts by destination depth. It
works, but verify it on the Dokploy host rather than assuming.

**Could not verify:** the tree's actual dstack version. `src/dstack/version.py`
is the CI placeholder `0.0.0`, so "0.20.29" rests on `CARBONTEQ_FORK.md` and the
Dockerfile's pinned base digest, not the source. Export/import is a recent
feature; confirm the pin before depending on it further.

**Peripheral, but know it:** the global SSH host-uniqueness check runs only in
the fleet plan path and in `_update_fleet`'s added-hosts branch, not in
`_create_fleet`, so a direct create-API call can double-register a host. And
removing an import terminates running jobs on those instances with
`INSTANCE_ACCESS_REVOKED` — an emergency lever, never a routine one.

## Verification

- `uv run pytest policy/tests` — the plugin's own suite, including
  `test_shipped_config.py`, which cross-checks the two deployed config files.
- `uv run pytest src/tests/_internal/server/routers/test_runs.py::TestSubmitRun`
  — includes the two tests covering the fork delta. Both fail with the delta
  reverted, which was checked rather than assumed.
- `uv run pytest`, plus `--runpostgres` before shipping, per the fork ledger.
- `uv run ruff check .` and `uv run ruff format --check .`.
- End to end, after deploying: apply inside and outside a window; re-apply a
  running run with a raised `priority` and confirm the stored value is re-banded;
  confirm a `team-research` member's `dstack ps` shows no other team's runs; and
  re-run the cancellation smoke test from the deployment README to prove the
  restructuring did not disturb the fork's runner delta.
