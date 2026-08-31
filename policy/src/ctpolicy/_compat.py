"""Every import that reaches into `dstack._internal`, in one place.

`dstack.plugins` is the supported plugin surface, but it is thin: it re-exports
the spec models and nothing else. The two helpers below live under `_internal`,
which upstream is free to move. Centralizing them means a rebase break shows up
as one failing import in one file rather than scattered across the package.

- `Duration` parses "12h" / "30m" / "1d" / plain seconds exactly as dstack parses
  the same strings in a run configuration, so policy.yaml and .dstack.yml cannot
  disagree about what "12h" means.
- `BackendType.REMOTE` is dstack's name for SSH/on-prem instances. It is the
  value the plugin pins `backends` to when a team may not use cloud.

Upstream also documents the plugin system itself as experimental — "Backward
compatibility is not guaranteed across releases"
(mkdocs/docs/reference/plugins/python/index.md) — so the `ApplyPolicy` hook
signature is the other thing to re-check on every rebase. See CARBONTEQ_POLICY.md.
"""

from dstack._internal.core.models.backends.base import BackendType
from dstack._internal.core.models.common import Duration

__all__ = ["BackendType", "Duration"]
