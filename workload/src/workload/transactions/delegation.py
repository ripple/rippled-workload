"""Delegation transaction generators for the antithesis workload."""

from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.models.transactions import DelegateSet
from xrpl.models.transactions.delegate_set import (
    GranularPermission,
    NON_DELEGABLE_TRANSACTIONS,
    Permission,
)
from xrpl.models.transactions.types import TransactionType

from workload import logging, params
from workload.models import UserAccount
from workload.randoms import choice, randint, sample
from workload.submit import submit_tx

DELEGABLE_TX_TYPES = [t for t in TransactionType if t not in NON_DELEGABLE_TRANSACTIONS]

log = logging.getLogger(__name__)


async def delegate_set(accounts: dict[str, UserAccount], client: AsyncJsonRpcClient) -> None:
    if params.should_send_faulty():
        return await _delegate_set_faulty(accounts, client)
    return await _delegate_set_valid(accounts, client)


async def _delegate_set_valid(accounts: dict[str, UserAccount], client: AsyncJsonRpcClient) -> None:
    src_id, delegate_id = sample(list(accounts), 2)
    src = accounts[src_id]
    perm_type = choice(["granular", "transaction_type", "mixed"])
    if perm_type == "granular":
        pool = list(GranularPermission)
    elif perm_type == "transaction_type":
        pool = DELEGABLE_TX_TYPES
    else:
        pool = list(GranularPermission) + DELEGABLE_TX_TYPES
    num_perms = min(len(pool), randint(1, 3))
    selected = sample(pool, num_perms)
    permissions = [Permission(permission_value=p) for p in selected]
    txn = DelegateSet(
        account=src.address,
        authorize=delegate_id,
        permissions=permissions,
    )
    await submit_tx("DelegateSet", txn, client, src.wallet)


async def _delegate_set_faulty(
    accounts: dict[str, UserAccount], client: AsyncJsonRpcClient
) -> None:
    if not accounts:
        return
    mutation = choice([
        "non_existent_authorize",
        "empty_permissions",
        "non_owner_submission",
    ])
    if mutation == "non_existent_authorize":
        src = choice(list(accounts.values()))
        all_perms = list(GranularPermission)
        num_perms = min(len(all_perms), randint(1, 3))
        selected = sample(all_perms, num_perms)
        permissions = [Permission(permission_value=p) for p in selected]
        txn = DelegateSet(
            account=src.address,
            authorize=params.fake_account(),
            permissions=permissions,
        )
        await submit_tx("DelegateSet", txn, client, src.wallet)

    elif mutation == "empty_permissions":
        src_id, delegate_id = sample(list(accounts), 2)
        src = accounts[src_id]
        txn = DelegateSet(
            account=src.address,
            authorize=delegate_id,
            permissions=[],
        )
        await submit_tx("DelegateSet", txn, client, src.wallet)

    elif mutation == "non_owner_submission":
        src_id, delegate_id = sample(list(accounts), 2)
        non_owners = [a for a in accounts.values() if a.address != src_id]
        if not non_owners:
            return
        impostor = choice(non_owners)
        all_perms = list(GranularPermission)
        num_perms = min(len(all_perms), randint(1, 3))
        selected = sample(all_perms, num_perms)
        permissions = [Permission(permission_value=p) for p in selected]
        txn = DelegateSet(
            account=src_id,
            authorize=delegate_id,
            permissions=permissions,
        )
        await submit_tx("DelegateSet", txn, client, impostor.wallet)



# ── Delegation helper for submit_tx ──────────────────────────────────

# Names of non-delegable transaction types for fast lookup.
_NON_DELEGABLE_NAMES: set[str] = {t.value for t in NON_DELEGABLE_TRANSACTIONS}


def maybe_delegate(
    tx_type: str,
    src_address: str,
    delegates: list,
    accounts: dict[str, UserAccount],
) -> tuple[str | None, object | None]:
    """Possibly pick a delegate to submit *tx_type* on behalf of *src_address*.

    Returns ``(delegate_address, delegate_wallet)`` when delegation should
    happen, or ``(None, None)`` otherwise.

    Called from ``submit_tx`` — no handler changes required.
    """
    from workload.randoms import random as _random

    if tx_type in _NON_DELEGABLE_NAMES:
        return None, None
    if not delegates:
        return None, None
    if _random() >= 0.10:
        return None, None

    candidates = [
        d for d in delegates
        if d.source == src_address
        and tx_type in d.permissions
        and d.delegate_address in accounts
    ]
    if not candidates:
        return None, None
    d = choice(candidates)
    acct = accounts[d.delegate_address]
    return acct.address, acct.wallet