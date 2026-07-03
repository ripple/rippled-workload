"""Feature gates for xrpl-py-branch-tracked functionality.

Workload branches track different rippled feature branches, and each new
amendment's xrpl-py support lives on its own, mutually incompatible xrpl-py
branch — importing a feature's xrpl models when the pinned xrpl-py branch
doesn't carry them crashes at import time (see workload/pyproject.toml
[tool.uv.sources]). Each gate below names the xrpl-py branch + rippled
amendment it requires. Env var overrides let docker compose flip a gate
without a code edit.
"""

from __future__ import annotations

import os


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "")


# XLS-0096 Confidential MPT: needs rippled's `ConfidentialTransfer` amendment. The
# pinned `pre-3.3-release-group` xrpl-py carries the models, so this is a runtime toggle;
# default off until a ConfidentialTransfer-enabled xrpld run turns it on via the env var.
CONFIDENTIAL_MPT = _env_flag("WORKLOAD_FEATURE_CONFIDENTIAL_MPT", False)

# XLS-68 Sponsored Fees & Reserves: needs rippled's `Sponsor` amendment. Models are on
# the pinned `pre-3.3-release-group` xrpl-py.
SPONSOR = _env_flag("WORKLOAD_FEATURE_SPONSOR", True)
