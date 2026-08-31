# ctpolicy

The CarbonTeq team and user policy layer for the dstack server: a `dstack.plugins`
entry point that enforces compute windows, run-duration ceilings, cloud
permission and two-level priority at submission time.

This package is **additive** — upstream dstack has no `policy/` directory, so it
carries no rebase conflict. The reasoning behind the design, and the one change
it needs inside dstack itself, are in [`CARBONTEQ_POLICY.md`](../CARBONTEQ_POLICY.md).

## What it does

`dstack apply` reaches `apply_plugin_policies()` twice — once to build the plan
and once to apply it — and this plugin runs on both. On the plan call, the CLI
renders the *effective* spec, so a user sees the clamped values before
confirming; on the apply call, the returned spec is what persists.

For each run it:

1. resolves the project to a team, rejecting projects that are neither a
   configured team nor explicitly exempt;
2. resolves the user to an effective policy, rejecting users with no entry;
3. rejects the run if the team is outside its compute window;
4. clamps `max_duration` to the tighter of the team's ceiling and the time left
   in the window;
5. pins `backends` to on-prem for teams without cloud permission, or narrows
   them to the team's cloud allowlist, and clamps `max_price`;
6. rewrites `priority` into the team's band.

It also rejects cloud fleets, volumes and gateways from teams without cloud
permission.

## Components

| Piece | Runs as | Does |
| --- | --- | --- |
| `ctpolicy` plugin | inside the dstack server | admission control and spec clamping |
| `ctpolicy-enforce` | its own container | writes the usage snapshot; stops runs that outlived their policy |
| `dstack-quota` | a command | read-only view of limits and usage |

All phases of the plan are implemented. The enforcer ships with
`DSTACK_CT_ENFORCER_DRY_RUN=true` in the deployment, so it writes snapshots —
which is what makes budgets enforceable at admission — but stops nothing until
that is turned off.

## Configuration

One YAML file, read from `$DSTACK_CT_POLICY_FILE` (default
`/etc/ctpolicy/policy.yaml`). The deployed copy lives at
`docker/server/carbonteq/policy.yaml`.

```yaml
version: 1                      # only 1 is understood
timezone: Asia/Karachi          # windows and budget periods use this zone

on_usage_unavailable: deny      # deny | allow, when the snapshot is missing/stale
usage_snapshot_max_age: 180s    # must exceed the enforcer's interval

ungoverned_projects:            # projects deliberately outside the policy layer
  - main

bands:                          # disjoint slices of dstack's 0-100 priority range
  team-research: [70, 99]
  team-platform: [40, 69]
  team-infra: [0, 39]

teams:
  team-research:                # must equal a dstack project name
    defaults:                   # the team-wide policy
      windows:
        - days: [mon, tue, wed, thu, fri]
          from: "08:00"
          to: "20:00"
      max_run_duration: 12h
      time_budget:              # the team's pool of run time
        period: month           # month | week
        limit: 400h
      cloud:
        allowed: true
        max_price: 4.0          # dollars per hour, per instance
        dollar_budget:          # cloud spend only; on-prem is priced at zero
          period: month
          limit: 1500
        backends: [nebius]      # which CLOUD backends; on-prem stays reachable
    users:                      # a user must be listed here to run
      alice: {}                 # `{}` inherits the team defaults
      bob:
        max_run_duration: 24h   # user keys override the team's
        time_budget: {period: week, limit: 20h}
        cloud:
          allowed: false
```

### Budgets

`time_budget` counts every run; `dollar_budget` counts only money, and dstack
prices SSH/on-prem instances at zero, so on-prem usage cannot consume it. That is
requirement 3 falling out of dstack's own cost model rather than being
special-cased.

A team's budget lives in its `defaults` and is measured against the whole team's
usage. A budget written under a user is measured against that user alone, so it
carves a slice out of the team pool rather than replacing it: **both must have
room, and the tighter one bounds the run.**

Admission subtracts two things, not one:

```
remaining = limit − spent − committed
```

`committed` is what active runs are still entitled to consume under the ceilings
they were admitted with. Without it, several runs each individually under budget
could collectively exceed it. Because `max_duration` is enforced by the runner
inside the VM, that entitlement is a real bound rather than an estimate.

What happens when a budget runs out differs by kind, deliberately:

* **Time budget exhausted** → the run is rejected. There is nothing else to do
  with it.
* **Dollar budget exhausted** → the run is *pinned to the on-prem fleet* and
  admitted. Running out of money stops a team spending, not working. A run that
  explicitly asked for a cloud backend is rejected instead, so the fallback is
  never silent.

Otherwise the budget clamps rather than rejects: `max_duration` is tightened to
what the time budget affords, and — only for a run that can actually reach a paid
backend — to what the dollar budget affords at the run's `max_price`.

### Usage accounting

Time is measured from a job submission's `submitted_at` to its `finished_at`,
which is exactly how dstack computes `Run.cost`. Keeping the two consistent
matters more than excluding queue time, and it errs high: a run that waited is
charged for waiting. A multi-node run accrues once per job submission, because
that is the resource it holds.

The apply hook cannot query the database — it runs on a worker thread of the
server's shared executor while the request still holds a session — so the
enforcer recomputes usage on its own schedule and writes a small JSON snapshot
that the hook reads. The snapshot is a **cache, never an authority**: every value
in it is derived from dstack's own records.

A team with no budget never reads the snapshot at all, so a stopped enforcer
cannot affect teams that configure none.

### Merging

A user's entry overrides their team's `defaults` key by key — that is what
"user-level policies override team-level ones" means, and an admin editing this
file is the only one who can widen anything.

`cloud` merges **key by key** rather than wholesale. Replacing the whole block
would mean that `cloud: {allowed: false}` on a user silently discarded the
team's `max_price` and `backends`, which widens rather than narrows. Only the
keys a user actually sets are overridden.

### Windows

A window is a recurring local-time interval on a set of weekdays. `from` is
inclusive, `to` is exclusive.

- `to: "24:00"` means the end of the day.
- If `to` is earlier than `from`, the window wraps past midnight and `days`
  names the day it *starts* on: `days: [mon], from: "20:00", to: "06:00"` runs
  from Monday evening into Tuesday morning.
- Adjacent and overlapping windows are merged, so back-to-back windows form one
  continuous span and a run is not truncated at the seam.
- Omitting `windows` entirely means always open.

Windows are evaluated in `timezone`. Zones with DST transitions are not
specially handled; `Asia/Karachi` has none.

### Priority bands

dstack stores a single `priority` integer per run and orders the queue by
`priority DESC` **globally across projects**. Giving each team a disjoint band
therefore makes any run of a higher-priority team sort ahead of every run of a
lower-priority one, while a run's own priority orders it only within its team.

A user's `priority` stays 0-100 and is mapped proportionally into their band
(`low + requested * (width - 1) // 100`), so nobody needs to know their band's
width. The mapping floors rather than rounds: monotonic, exact at both ends, and
free of round-half-to-even surprises.

**The ceiling:** teams × per-team granularity must fit in 101 values. Three
teams leaves ~30 job-priority levels each; ten teams leaves ~10; past roughly
fifty teams the scheme stops being meaningful.

**What this does not give you:** dstack's ordering is best-effort. A
high-priority run that cannot be *placed* does not block a lower-priority run
that fits. See `CARBONTEQ_POLICY.md` for why that was accepted.

### Fail-closed behaviour

| Situation | Result |
| --- | --- |
| Project is neither a team nor in `ungoverned_projects` | rejected |
| User not listed under their team's `users` | rejected |
| Policy file missing, unreadable, or malformed | every submission rejected with the parse error |
| Unknown key anywhere in the file | rejected at load |

The last two matter most: a broken policy file blocks submissions rather than
falling back to a stale or permissive policy. The rejection text names the file
and the problem.

## Reloading

The file is re-read whenever its mtime or size changes, so a policy edit takes
effect on the next `dstack apply` — no server restart. This is deliberately
unlike `server/config.yml`, which dstack applies once at boot and never re-reads.

The parse is cached because the hook runs on the server's shared executor
threads; the hot path is a `stat` plus a dict lookup.

## Development

```sh
uv sync --all-extras          # from the repo root, once
uv run pytest policy/tests    # the root pytest testpaths only covers src/tests
uv run ruff check policy
uv run ruff format --check policy
```

`policy/tests/test_shipped_config.py` cross-checks the two deployed files
(`docker/server/carbonteq/policy.yaml` and `config.yml`) against each other:
every team must be a project, every project must be governed or explicitly
exempt, the plugin must be enabled, and a team that forbids cloud must not have
cloud backends declared. Drift between them would otherwise only surface in
production as a blanket rejection.

## Dependencies

Deliberately none. `docker/server/carbonteq/Dockerfile` installs this wheel with
`uv pip install --no-deps` so the server image keeps upstream's immutable,
already-qualified dependency graph. Everything ctpolicy imports — pydantic v1,
PyYAML, dstack — is already in the base image. Adding a dependency here would do
nothing at build time and then fail at import inside the container.

Imports that reach into `dstack._internal` are confined to `_compat.py` so a
rebase break surfaces as one failing import in one file.
