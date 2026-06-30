"""AccountSet transaction generators: set/clear account flags."""

from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.models.transactions import AccountSet, AccountSetAsfFlag
from xrpl.wallet import Wallet

from workload import params
from workload.fuzz import submit_fuzzed
from workload.models import UserAccount
from workload.randoms import choice, random
from workload.submit import submit_raw, submit_tx

# Flags that affect trust lines and payments.
INTERESTING_FLAGS: list[AccountSetAsfFlag] = [
    AccountSetAsfFlag.ASF_DEFAULT_RIPPLE,
    AccountSetAsfFlag.ASF_REQUIRE_AUTH,
    AccountSetAsfFlag.ASF_REQUIRE_DEST,
    AccountSetAsfFlag.ASF_DISALLOW_XRP,
    AccountSetAsfFlag.ASF_GLOBAL_FREEZE,
    AccountSetAsfFlag.ASF_NO_FREEZE,
    AccountSetAsfFlag.ASF_DISABLE_INCOMING_TRUSTLINE,
    AccountSetAsfFlag.ASF_DISABLE_INCOMING_NFTOKEN_OFFER,
    AccountSetAsfFlag.ASF_DISABLE_INCOMING_PAYCHAN,
    AccountSetAsfFlag.ASF_DISABLE_INCOMING_CHECK,
]


async def account_set_random(accounts: dict[str, UserAccount], client: AsyncJsonRpcClient) -> None:
    if params.should_send_faulty():
        return await _account_set_faulty(accounts, client)
    return await _account_set_valid(accounts, client)


def _account_set_base(
    accounts: dict[str, UserAccount],
) -> tuple[AccountSet, Wallet] | None:
    """Valid AccountSet (set or clear one flag) + wallet; shared by valid and fuzz."""
    if not accounts:
        return None
    account = accounts[choice(list(accounts))]
    flag = choice(INTERESTING_FLAGS)
    if random() < 0.5:
        txn = AccountSet(account=account.address, set_flag=flag)
    else:
        txn = AccountSet(account=account.address, clear_flag=flag)
    return txn, account.wallet


async def _account_set_valid(accounts: dict[str, UserAccount], client: AsyncJsonRpcClient) -> None:
    built = _account_set_base(accounts)
    if built is None:
        return
    txn, wallet = built
    await submit_tx("AccountSet", txn, client, wallet)


async def _account_set_faulty(accounts: dict[str, UserAccount], client: AsyncJsonRpcClient) -> None:
    built = _account_set_base(accounts)
    if built is None:
        return
    base, wallet = built

    # All curated AccountSet malformations are tem (preflight, never enter the
    # ledger): xrpl-py rejects bad TransferRate/TickSize and set==clear at
    # construction, so they go through submit_raw.
    mutation = choice(["bad_transfer_rate", "bad_tick_size", "set_clear_same_flag", "fuzz"])
    if mutation == "fuzz":
        await submit_fuzzed("AccountSet", base, client, wallet)
        return

    if mutation == "bad_transfer_rate":
        # Valid range is 0 or 1e9..2e9 -> below 1e9 (but nonzero) is temBAD_TRANSFER_RATE.
        def _mutate(d: dict) -> None:
            d["TransferRate"] = 1

    elif mutation == "bad_tick_size":
        # Valid range is 0 or 3..15 -> 255 is temBAD_TICK_SIZE.
        def _mutate(d: dict) -> None:
            d["TickSize"] = 255

    else:  # set_clear_same_flag
        # SetFlag == ClearFlag -> temINVALID_FLAG.
        flag = int(choice(INTERESTING_FLAGS))

        def _mutate(d: dict) -> None:
            d.pop("SetFlag", None)
            d.pop("ClearFlag", None)
            d["SetFlag"] = flag
            d["ClearFlag"] = flag

    await submit_raw("AccountSet", base, client, wallet, _mutate)
