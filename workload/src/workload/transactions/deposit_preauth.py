"""DepositPreauth handler — preauthorize a peer to send to a deposit-auth account.

Authorize-only (never Unauthorize), so a tesSUCCESS always Creates a DepositPreauth
ledger entry (assertions._META_EXPECTATIONS). Reserve-sponsorable + delegable +
ticket-OK, so the submit-time modifier pipeline decorates it freely.
"""

from __future__ import annotations

from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.models.transactions import DepositPreauth
from xrpl.wallet import Wallet

from workload import params
from workload.fuzz import submit_fuzzed
from workload.models import UserAccount
from workload.randoms import choice, sample
from workload.submit import submit_tx


async def deposit_preauth(accounts: dict[str, UserAccount], client: AsyncJsonRpcClient) -> None:
    if params.should_send_faulty():
        return await _deposit_preauth_faulty(accounts, client)
    return await _deposit_preauth_valid(accounts, client)


def _deposit_preauth_base(
    accounts: dict[str, UserAccount],
) -> tuple[DepositPreauth, Wallet] | None:
    """Valid DepositPreauth (A preauthorizes an existing peer B) + wallet."""
    if len(accounts) < 2:
        return None
    src, target = sample(list(accounts.values()), 2)
    txn = DepositPreauth(account=src.address, authorize=target.address)
    return txn, src.wallet


async def _deposit_preauth_valid(
    accounts: dict[str, UserAccount], client: AsyncJsonRpcClient
) -> None:
    built = _deposit_preauth_base(accounts)
    if built is None:
        return
    txn, wallet = built
    await submit_tx("DepositPreauth", txn, client, wallet)


async def _deposit_preauth_faulty(
    accounts: dict[str, UserAccount], client: AsyncJsonRpcClient
) -> None:
    if len(accounts) < 2:
        return
    mutation = choice(["fuzz", "nonexistent_target", "preauth_self"])

    if mutation == "fuzz":
        built = _deposit_preauth_base(accounts)
        if built is None:
            return
        base, wallet = built
        await submit_fuzzed("DepositPreauth", base, client, wallet)
        return

    src = choice(list(accounts.values()))
    if mutation == "nonexistent_target":
        # Syntactically valid but unfunded AccountID -> preclaim tecNO_TARGET.
        txn = DepositPreauth(account=src.address, authorize=params.fake_account())
    else:  # preauth_self: authorize == account -> preflight temCANNOT_PREAUTH_SELF
        txn = DepositPreauth(account=src.address, authorize=src.address)

    await submit_tx("DepositPreauth", txn, client, src.wallet)
