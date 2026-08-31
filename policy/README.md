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

## Status

Phase 1 of the plan. Budgets (time and dollars), the usage snapshot, the
background enforcer and the `dstack-quota` companion CLI are **not** in this
build; `time_budget` and `dollar_budget` keys are rejected as unknown rather
than silently ignored. See the phased build order in `CARBONTEQ_POLICY.md`.

## Configuration

One YAML file, read from `$DSTACK_CT_POLICY_FILE` (default
`/etc/ctpolicy/policy.yaml`). The deployed copy lives at
`docker/server/carbonteq/policy.yaml`.

```yaml
version: 1                      # only 1 is understood
timezone: Asia/Karachi          # windows are evaluated in this zone

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
      cloud:
        allowed: true
        max_price: 4.0          # dollars per hour, per instance
        backends: [nebius]      # which CLOUD backends; on-prem stays reachable
    users:                      # a user must be listed here to run
      alice: {}                 # `{}` inherits the team defaults
      bob:
        max_run_duration: 24h   # user keys override the team's
        cloud:
          allowed: false
```

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
