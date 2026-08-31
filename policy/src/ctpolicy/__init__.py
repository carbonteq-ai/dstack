"""CarbonTeq team/user policy and quota layer for the dstack server.

Loaded by the dstack server through the `dstack.plugins` entry point declared in
this package's pyproject.toml, and enabled by listing `ctpolicy` under `plugins:`
in the server's config.yml. See policy/README.md and CARBONTEQ_POLICY.md.
"""

from ctpolicy.plugin import CtPolicy, PolicyPlugin

__all__ = ["CtPolicy", "PolicyPlugin"]
