"""Fire-and-forget transaction submission.

Autofills, signs, and submits a transaction via RPC without waiting for
validation. The WebSocket listener (ws_listener.py) observes validated
results and fires assertions / updates state.
"""

from collections.abc import Callable

from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.asyncio.transaction import autofill, autofill_and_sign, submit
from xrpl.core import keypairs
from xrpl.core.binarycodec import encode, encode_for_signing
from xrpl.models.requests import SubmitOnly
from xrpl.models.transactions.transaction import Transaction
from xrpl.wallet import Wallet

from workload import logging
from workload.assertions import tx_submitted

log = logging.getLogger(__name__)

# ── Delegation state (set once via configure()) ──────────────────────
_delegates: list = []
_accounts: dict = {}


def configure(delegates: list, accounts: dict) -> None:
    """Store references to workload delegation state.

    Called once during ``Workload.__init__`` so that ``submit_tx`` can
    transparently apply delegation without any handler changes.
    """
    global _delegates, _accounts
    _delegates = delegates
    _accounts = accounts


async def submit_tx(
    name: str,
    txn: Transaction,
    client: AsyncJsonRpcClient,
    wallet: Wallet,
    seq: int | None = None,
) -> dict:
    """Sign and submit a transaction. Returns the submit response result.

    The returned dict contains the preliminary (tentative) engine_result
    and the transaction hash. Final results arrive via the WS listener.

    If ``seq`` is provided, it is stamped onto the transaction before
    autofill so that xrpl-py skips the RPC sequence fetch.

    When delegation state is configured (via ``configure``), there is a
    10% chance that a matching delegate will sign on behalf of the
    source account for delegable transaction types.

    Raises XRPLRequestFailureException on RPC-level failures (connection
    refused, malformed request, etc.) — let it propagate to the endpoint
    handler's XRPLException catch.
    """
    if seq is not None:
        txn = txn.__replace__(sequence=seq)

    # Possibly delegate: lazy import to avoid circular dependency
    if _delegates:
        from workload.transactions.delegation import maybe_delegate

        delegate_addr, delegate_wallet = maybe_delegate(
            name,
            txn.account,
            _delegates,
            _accounts,
        )
        if delegate_addr is not None:
            txn = txn.__replace__(delegate=delegate_addr)
            wallet = delegate_wallet

    signed = await autofill_and_sign(txn, client, wallet)
    response = await submit(signed, client)
    result = response.result
    tx_submitted(name, txn, result)
    return result


async def submit_raw(
    name: str,
    base: Transaction,
    client: AsyncJsonRpcClient,
    wallet: Wallet,
    mutate: Callable[[dict], None] | None = None,
) -> dict:
    """Submit path for ``_faulty`` handlers — bypasses xrpl-py model validation.

    Autofill a VALID ``base`` model (to obtain Sequence/Fee/LastLedgerSequence),
    serialize it to an XRPL JSON dict, optionally apply ``mutate(dict)`` to
    introduce a malformation xrpl-py would reject at construction (tfHybrid
    without DomainID, empty/oversized/duplicate arrays, …), then sign and submit
    the raw blob so rippled's own preflight/preclaim is what rejects it.

    ``mutate`` is omitted when the faulty intent is already a valid model (e.g.
    a non-member submitting a well-formed domain offer) — the raw path is still
    used so every ``_faulty`` case shares one submission discipline.

    Wires ``tx_submitted`` exactly like ``submit_tx`` (seen + submit-time
    internal-error assertion). Use ONLY in ``_faulty`` paths.
    """
    autofilled = await autofill(base, client)
    tx_dict = autofilled.to_xrpl()
    tx_dict.pop("TxnSignature", None)
    if mutate is not None:
        mutate(tx_dict)
    tx_dict["SigningPubKey"] = wallet.public_key
    serialized = encode_for_signing(tx_dict)
    tx_dict["TxnSignature"] = keypairs.sign(bytes.fromhex(serialized), wallet.private_key)
    response = await client.request(SubmitOnly(tx_blob=encode(tx_dict)))
    result = response.result
    tx_submitted(name, tx_dict, result)
    return result
