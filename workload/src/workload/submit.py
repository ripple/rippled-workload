"""Fire-and-forget transaction submission.

Autofills, signs, and submits a transaction via RPC without waiting for
validation. The WebSocket listener (ws_listener.py) observes validated
results and fires assertions / updates state.
"""

from workload import logging
from workload.assertions import tx_submitted
from xrpl.asyncio.transaction import autofill_and_sign, submit
from xrpl.wallet import Wallet

log = logging.getLogger(__name__)


async def submit_tx(name: str, txn, client, wallet: Wallet) -> dict:
    """Sign and submit a transaction. Returns the submit response result.

    The returned dict contains the preliminary (tentative) engine_result
    and the transaction hash. Final results arrive via the WS listener.

    Raises XRPLRequestFailureException on RPC-level failures (connection
    refused, malformed request, etc.) — let it propagate to the endpoint
    handler's XRPLException catch.
    """
    tx_submitted(name, txn)
    signed = await autofill_and_sign(txn, client, wallet)
    response = await submit(signed, client)
    result = response.result
    preliminary = result.get("engine_result", "")
    tx_hash = result.get("tx_json", {}).get("hash", "")
    log.debug("Submitted %s: %s (hash=%s)", name, preliminary, tx_hash[:16])
    return result
