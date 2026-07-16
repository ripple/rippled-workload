"""Delegation transaction generators."""

from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.models.transactions import DelegateSet
from xrpl.models.transactions.delegate_set import (
    NON_DELEGABLE_TRANSACTIONS,
    GranularPermission,
    Permission,
)
from xrpl.models.transactions.types import TransactionType
from xrpl.wallet import Wallet

from workload import params
from workload.fuzz import submit_fuzzed
from workload.models import UserAccount
from workload.randoms import choice, randint, sample
from workload.submit import submit_tx

# xrpl-py's NON_DELEGABLE_TRANSACTIONS is stale vs rippled develop's transactions.macro:
# it omits SponsorshipTransfer and the whole Vault (XLS-65) + Loan/LoanBroker families,
# which develop marks Delegation::NotDelegable. A DelegateSet authorizing any of these
# draws temMALFORMED, so exclude them until xrpl-py catches up. (SponsorshipSet, the
# other sponsor tx, stays Delegable.)
_RIPPLED_NON_DELEGABLE_VALUES = {
    "SponsorshipTransfer",
    "VaultCreate",
    "VaultDeposit",
    "VaultWithdraw",
    "VaultSet",
    "VaultDelete",
    "VaultClawback",
    "LoanSet",
    "LoanDelete",
    "LoanManage",
    "LoanPay",
    "LoanBrokerSet",
    "LoanBrokerDelete",
    "LoanBrokerCoverDeposit",
    "LoanBrokerCoverWithdraw",
    "LoanBrokerCoverClawback",
}
DELEGABLE_TX_TYPES = [
    t
    for t in TransactionType
    if t not in NON_DELEGABLE_TRANSACTIONS and t.value not in _RIPPLED_NON_DELEGABLE_VALUES
]


async def delegate_set(accounts: dict[str, UserAccount], client: AsyncJsonRpcClient) -> None:
    if params.should_send_faulty():
        return await _delegate_set_faulty(accounts, client)
    return await _delegate_set_valid(accounts, client)


def _delegate_set_base(
    accounts: dict[str, UserAccount],
) -> tuple[DelegateSet, Wallet] | None:
    """Valid DelegateSet (grant a delegate a random permission set) + wallet."""
    if len(accounts) < 2:
        return None
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
    return txn, src.wallet


async def _delegate_set_valid(accounts: dict[str, UserAccount], client: AsyncJsonRpcClient) -> None:
    built = _delegate_set_base(accounts)
    if built is None:
        return
    txn, wallet = built
    await submit_tx("DelegateSet", txn, client, wallet)


async def _delegate_set_faulty(
    accounts: dict[str, UserAccount], client: AsyncJsonRpcClient
) -> None:
    if not accounts:
        return
    mutation = choice(
        [
            "fuzz",
            "non_existent_authorize",
            "empty_permissions",
            "non_owner_submission",
        ]
    )
    if mutation == "fuzz":
        built = _delegate_set_base(accounts)
        if built is None:
            return
        base, wallet = built
        await submit_fuzzed("DelegateSet", base, client, wallet)
        return

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

_NON_DELEGABLE_NAMES: set[str] = {t.value for t in NON_DELEGABLE_TRANSACTIONS}


def maybe_delegate(
    tx_type: str,
    src_address: str,
    delegates: list,
    accounts: dict[str, UserAccount],
) -> tuple[str | None, Wallet | None]:
    """Pick a delegate authorized for tx_type on behalf of src_address, or
    (None, None). Fire probability + non-delegable filtering are owned by the
    delegate Modifier (modifiers.py); this is a pure candidate picker."""
    if not delegates:
        return None, None

    candidates = [
        d
        for d in delegates
        if d.source == src_address and tx_type in d.permissions and d.delegate_address in accounts
    ]
    if not candidates:
        return None, None
    d = choice(candidates)
    acct = accounts[d.delegate_address]
    return acct.address, acct.wallet
