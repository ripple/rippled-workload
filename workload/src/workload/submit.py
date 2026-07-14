"""Fire-and-forget transaction submission; ws_listener.py handles validated results."""

from collections.abc import Callable

from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.asyncio.transaction import autofill, autofill_and_sign, submit
from xrpl.core import keypairs
from xrpl.core.binarycodec import encode, encode_for_signing
from xrpl.models.requests import SubmitOnly
from xrpl.models.transactions.transaction import Transaction
from xrpl.wallet import Wallet

from workload import logging
from workload.assertions import assert_modifier_combo, tx_submitted

log = logging.getLogger(__name__)

# ── Delegation/sponsorship state (set once via configure()) ──────────
_delegates: list = []
_accounts: dict = {}
_sponsorships: list = []


def configure(delegates: list, accounts: dict, sponsorships: list) -> None:
    """Store delegation/sponsorship state once at init so submit_tx can apply
    it transparently."""
    global _delegates, _accounts, _sponsorships
    _delegates = delegates
    _accounts = accounts
    _sponsorships = sponsorships


async def submit_tx(
    name: str,
    txn: Transaction,
    client: AsyncJsonRpcClient,
    wallet: Wallet,
    seq: int | None = None,
) -> dict:
    """Sign and submit; returns the tentative engine_result (final via WS listener).

    ``seq`` (if given) is stamped pre-autofill so xrpl-py skips the RPC seq fetch.
    Runs the transaction-modifier pipeline (ticket → delegate → sponsor); the
    sponsor modifier owns fee + reserve sponsorship (fold of the former inline
    fee-sponsor block) and may attach a post-sign co-sign hook.
    Lets XRPLRequestFailureException propagate to the handler's XRPLException catch.
    """
    if seq is not None:
        txn = txn.__replace__(sequence=seq)

    # Lazy import: modifiers -> transactions -> delegation -> submit would cycle.
    from workload.modifiers import ModifierCtx, apply_modifiers

    ctx = ModifierCtx(delegates=_delegates, accounts=_accounts, sponsorships=_sponsorships)
    txn, wallet, applied, cosigns = apply_modifiers(name, txn, wallet, ctx)
    assert_modifier_combo(name, applied)

    signed = await autofill_and_sign(txn, client, wallet)
    for cosign in cosigns:
        signed = cosign(signed)
    response = await submit(signed, client)
    result: dict = response.result
    tx_submitted(name, txn, result)
    return result


async def submit_raw(
    name: str,
    base: Transaction,
    client: AsyncJsonRpcClient,
    wallet: Wallet,
    mutate: Callable[[dict], None] | None = None,
) -> dict:
    """Raw ``_faulty`` submit path: autofill a valid ``base``, ``mutate(dict)`` it
    into a malformation xrpl-py rejects at construction, then sign+submit raw so
    rippled's preflight/preclaim does the rejecting.

    Contract: ``mutate`` MUST keep the dict encodable — submit_raw has no encode
    guard (only submit_fuzzed catches XRPLBinaryCodecException / ValueError).
    Delegation is intentionally NOT applied here so a flagged result maps to one account.
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
    result: dict = response.result
    tx_submitted(name, tx_dict, result)
    return result
