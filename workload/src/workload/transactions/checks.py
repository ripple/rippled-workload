"""CheckCreate / CheckCash / CheckCancel workload handlers."""

from __future__ import annotations

from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.models.transactions import CheckCancel, CheckCash, CheckCreate
from xrpl.wallet import Wallet

from workload import params
from workload.fuzz import submit_fuzzed
from workload.models import Check, UserAccount
from workload.randoms import choice, randint
from workload.submit import submit_tx

# ── CheckCreate ─────────────────────────────────────────────────────


async def check_create(
    accounts: dict[str, UserAccount],
    checks: list[Check],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _check_create_faulty(accounts, client)
    return await _check_create_valid(accounts, checks, client)


def _check_create_base(
    accounts: dict[str, UserAccount],
) -> tuple[CheckCreate, Wallet] | None:
    """Valid CheckCreate + wallet; shared by valid and fuzz."""
    if not accounts:
        return None

    acct_list = list(accounts.values())
    src = choice(acct_list)
    dst = choice(acct_list)

    send_max = params.check_send_max()

    txn = CheckCreate(
        account=src.address,
        destination=dst.address,
        send_max=send_max,
    )
    return txn, src.wallet


async def _check_create_valid(
    accounts: dict[str, UserAccount],
    checks: list[Check],
    client: AsyncJsonRpcClient,
) -> None:
    built = _check_create_base(accounts)
    if built is None:
        return
    txn, wallet = built
    result = await submit_tx("CheckCreate", txn, client, wallet)

    # Track only when the submit will likely apply (state updater later swaps
    # the hash placeholder for the real check_id), else the placeholder leaks.
    engine = result.get("engine_result", "") if result else ""
    if engine in ("tesSUCCESS", "terQUEUED", "terPRE_SEQ"):
        tx_json = result.get("tx_json", result)
        check_id = tx_json.get("hash", "")
        # The base always builds an XRP-drops str send_max; narrow for Check.send_max.
        if check_id and isinstance(txn.send_max, str):
            checks.append(
                Check(
                    check_id=check_id,
                    creator=txn.account,
                    destination=txn.destination,
                    send_max=txn.send_max,
                )
            )


async def _check_create_faulty(
    accounts: dict[str, UserAccount],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return
    src = choice(list(accounts.values()))

    mutation = choice(
        [
            "fuzz",
            "zero_send_max",
            "self_destination",
            "non_existent_destination",
            "past_expiration",
        ]
    )
    if mutation == "fuzz":
        built = _check_create_base(accounts)
        if built is None:
            return
        base, wallet = built
        await submit_fuzzed("CheckCreate", base, client, wallet)
        return

    if mutation == "zero_send_max":
        txn = CheckCreate(
            account=src.address,
            destination=src.address,
            send_max="0",
        )
    elif mutation == "self_destination":
        txn = CheckCreate(
            account=src.address,
            destination=src.address,
            send_max=params.check_send_max(),
        )
    elif mutation == "non_existent_destination":
        txn = CheckCreate(
            account=src.address,
            destination=params.fake_account(),
            send_max=params.check_send_max(),
        )
    else:  # past_expiration
        past = params._ripple_now() - randint(100, 10_000)
        txn = CheckCreate(
            account=src.address,
            destination=src.address,
            send_max=params.check_send_max(),
            expiration=past,
        )

    await submit_tx("CheckCreate", txn, client, src.wallet)


# ── CheckCash ───────────────────────────────────────────────────────


async def check_cash(
    accounts: dict[str, UserAccount],
    checks: list[Check],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _check_cash_faulty(accounts, checks, client)
    return await _check_cash_valid(accounts, checks, client)


def _check_cash_base(
    accounts: dict[str, UserAccount],
    checks: list[Check],
) -> tuple[CheckCash, Wallet] | None:
    """Valid CheckCash of a tracked check + wallet; shared by valid and fuzz."""
    if not checks:
        return None

    check = choice(checks)
    # Only the destination can cash a check.
    dst = accounts.get(check.destination)
    if not dst:
        return None

    cash_amount = params.check_cash_amount(check.send_max)

    if choice([True, False]):
        txn = CheckCash(
            account=dst.address,
            check_id=check.check_id,
            amount=cash_amount,
        )
    else:
        txn = CheckCash(
            account=dst.address,
            check_id=check.check_id,
            deliver_min=cash_amount,
        )
    return txn, dst.wallet


async def _check_cash_valid(
    accounts: dict[str, UserAccount],
    checks: list[Check],
    client: AsyncJsonRpcClient,
) -> None:
    built = _check_cash_base(accounts, checks)
    if built is None:
        return
    txn, wallet = built
    await submit_tx("CheckCash", txn, client, wallet)


async def _check_cash_faulty(
    accounts: dict[str, UserAccount],
    checks: list[Check],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return
    src = choice(list(accounts.values()))

    mutation = choice(
        [
            "fuzz",
            "fake_check_id",
            "zero_amount",
            "non_destination_cash",
        ]
    )
    if mutation == "fuzz":
        built = _check_cash_base(accounts, checks)
        if built is None:
            return
        base, wallet = built
        await submit_fuzzed("CheckCash", base, client, wallet)
        return

    if mutation == "fake_check_id":
        txn = CheckCash(
            account=src.address,
            check_id=params.fake_id(),
            amount="1000000",
        )
    elif mutation == "zero_amount":
        txn = CheckCash(
            account=src.address,
            check_id=params.fake_id(),
            amount="0",
        )
    else:  # non_destination_cash
        txn = CheckCash(
            account=src.address,
            check_id=params.fake_id(),
            amount=params.check_send_max(),
        )

    await submit_tx("CheckCash", txn, client, src.wallet)


# ── CheckCancel ─────────────────────────────────────────────────────


async def check_cancel(
    accounts: dict[str, UserAccount],
    checks: list[Check],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _check_cancel_faulty(accounts, checks, client)
    return await _check_cancel_valid(accounts, checks, client)


def _check_cancel_base(
    accounts: dict[str, UserAccount],
    checks: list[Check],
) -> tuple[CheckCancel, Wallet] | None:
    """Valid CheckCancel of a tracked check + wallet; shared by valid and fuzz."""
    if not checks:
        return None

    check = choice(checks)
    # Creator or destination can cancel.
    canceller_addr = choice([check.creator, check.destination])
    canceller = accounts.get(canceller_addr)
    if not canceller:
        return None

    txn = CheckCancel(
        account=canceller.address,
        check_id=check.check_id,
    )
    return txn, canceller.wallet


async def _check_cancel_valid(
    accounts: dict[str, UserAccount],
    checks: list[Check],
    client: AsyncJsonRpcClient,
) -> None:
    built = _check_cancel_base(accounts, checks)
    if built is None:
        return
    txn, wallet = built
    await submit_tx("CheckCancel", txn, client, wallet)


async def _check_cancel_faulty(
    accounts: dict[str, UserAccount],
    checks: list[Check],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return
    src = choice(list(accounts.values()))

    mutation = choice(
        [
            "fuzz",
            "fake_check_id",
            "cancel_others_check",
        ]
    )
    if mutation == "fuzz":
        built = _check_cancel_base(accounts, checks)
        if built is None:
            return
        base, wallet = built
        await submit_fuzzed("CheckCancel", base, client, wallet)
        return

    if mutation == "fake_check_id":
        txn = CheckCancel(
            account=src.address,
            check_id=params.fake_id(),
        )
    else:  # cancel_others_check
        txn = CheckCancel(
            account=src.address,
            check_id=params.fake_id(),
        )

    await submit_tx("CheckCancel", txn, client, src.wallet)
