"""Fire-and-forget transaction submission; ws_listener.py handles validated results."""

from collections.abc import Callable

from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.asyncio.transaction import autofill, autofill_and_sign, submit
from xrpl.core import keypairs
from xrpl.core.binarycodec import encode, encode_for_signing
from xrpl.models.requests import SubmitOnly
from xrpl.models.transactions.transaction import Transaction
from xrpl.wallet import Wallet

from workload import features, logging, params
from workload.assertions import tx_submitted

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
    With delegation configured, a matching delegate co-signs ~10% of the time.
    Independently, ~10% of eligible txns get a prefunded fee sponsor attached
    (mutually exclusive with delegation). Lets XRPLRequestFailureException
    propagate to the handler's XRPLException catch.
    """
    if seq is not None:
        txn = txn.__replace__(sequence=seq)

    # Lazy import: avoids circular dependency.
    delegated = False
    if _delegates:
        from workload.transactions.delegation import maybe_delegate

        delegate_addr, delegate_wallet = maybe_delegate(
            name,
            txn.account,
            _delegates,
            _accounts,
        )
        if delegate_addr is not None and delegate_wallet is not None:
            txn = txn.__replace__(delegate=delegate_addr)
            wallet = delegate_wallet
            delegated = True

    # Batch's Fee must be zero (TapBatch), and a txn built with sponsor fields
    # already set (e.g. sponsorship.py's prefunded co-sign helpers) must not be
    # clobbered here.
    if (
        not delegated
        and features.SPONSOR
        and _sponsorships
        and name != "Batch"
        and txn.sponsor is None
    ):
        from workload.transactions.sponsorship import maybe_sponsor

        sponsor_addr = maybe_sponsor(txn.account, _sponsorships)
        if sponsor_addr is not None:
            txn = txn.__replace__(sponsor=sponsor_addr, sponsor_flags=params.SPF_SPONSOR_FEE)

    signed = await autofill_and_sign(txn, client, wallet)
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
