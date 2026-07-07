"""Post-fault liveness probe: a payment must validate once faults stop."""

from __future__ import annotations

from typing import TYPE_CHECKING

from xrpl.asyncio.transaction import submit_and_wait
from xrpl.models.transactions import Payment

from workload.assertions import assert_network_functional
from workload.randoms import choice

if TYPE_CHECKING:
    from workload.app import Workload


async def probe_network(w: Workload) -> bool:
    """Submit a payment and wait for validation; feed the liveness assertion. Driven
    by an ``eventually_*`` script after faults stop, so a healthy network must let at
    least one probe through — ``sometimes(network_functional_after_faults)``."""
    accts = list(w.accounts.values())
    src = choice(accts)
    others = [a for a in accts if a.address != src.address]
    if not others:
        return False
    dst = choice(others)
    txn = Payment(account=src.address, destination=dst.address, amount="1000")
    ok = False
    engine = ""
    try:
        resp = await submit_and_wait(txn, w.client, src.wallet)
        meta = resp.result.get("meta", {})
        engine = str(meta.get("TransactionResult", "")) if isinstance(meta, dict) else ""
        ok = bool(resp.result.get("validated")) and engine == "tesSUCCESS"
    except Exception:
        ok = False
    assert_network_functional(ok, engine)
    return ok
