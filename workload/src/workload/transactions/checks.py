"""CheckCreate / CheckCash / CheckCancel workload handlers.

A Check is a deferred payment: the creator authorises up to `send_max` to be
pulled by the destination.  The destination cashes it for an exact `amount` or
a flexible `deliver_min`.  Either party (or anyone after expiration) can cancel.

State tracking keeps (check_id, creator, destination, send_max) so
CheckCash and CheckCancel can reference live checks.
"""

from __future__ import annotations

from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.models.transactions import CheckCancel, CheckCash, CheckCreate

from workload import params
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


async def _check_create_valid(
    accounts: dict[str, UserAccount],
    checks: list[Check],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return

    acct_list = list(accounts.values())
    src = choice(acct_list)
    dst = choice(acct_list)

    send_max = params.check_send_max()

    txn = CheckCreate(
        account=src.address,
        destination=dst.address,
        send_max=send_max,
    )
    result = await submit_tx("CheckCreate", txn, client, src.wallet)

    # Optimistically track — check_id comes from state updater,
    # but we store a placeholder so CheckCash/Cancel have entries.
    if result:
        tx_json = result.get("tx_json", result)
        # check_id = hash of the tx; the real one comes from meta
        check_id = tx_json.get("hash", "")
        if check_id:
            checks.append(Check(
                check_id=check_id,
                creator=src.address,
                destination=dst.address,
                send_max=send_max,
            ))


async def _check_create_faulty(
    accounts: dict[str, UserAccount],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return
    src = choice(list(accounts.values()))

    mutation = choice([
        "zero_send_max",
        "self_destination",
        "non_existent_destination",
        "past_expiration",
    ])

    if mutation == "zero_send_max":
        txn = CheckCreate(
            account=src.address,
            destination=src.address,
            send_max="0",
        )
    elif mutation == "self_destination":
        # Create check to self (may or may not be valid depending on config)
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
        return await _check_cash_faulty(accounts, client)
    return await _check_cash_valid(accounts, checks, client)


async def _check_cash_valid(
    accounts: dict[str, UserAccount],
    checks: list[Check],
    client: AsyncJsonRpcClient,
) -> None:
    if not checks:
        return

    check = choice(checks)
    # Only the destination can cash a check
    dst = accounts.get(check.destination)
    if not dst:
        return

    # Randomly use exact amount or deliver_min
    use_exact = choice([True, False])
    cash_amount = params.check_cash_amount(check.send_max)

    if use_exact:
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

    await submit_tx("CheckCash", txn, client, dst.wallet)


async def _check_cash_faulty(
    accounts: dict[str, UserAccount],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return
    src = choice(list(accounts.values()))

    mutation = choice([
        "fake_check_id",
        "zero_amount",
        "non_destination_cash",
    ])

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
    else:  # non_destination_cash — wrong account cashes
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
        return await _check_cancel_faulty(accounts, client)
    return await _check_cancel_valid(accounts, checks, client)


async def _check_cancel_valid(
    accounts: dict[str, UserAccount],
    checks: list[Check],
    client: AsyncJsonRpcClient,
) -> None:
    if not checks:
        return

    check = choice(checks)
    # Creator or destination can cancel; pick one
    canceller_addr = choice([check.creator, check.destination])
    canceller = accounts.get(canceller_addr)
    if not canceller:
        return

    txn = CheckCancel(
        account=canceller.address,
        check_id=check.check_id,
    )
    await submit_tx("CheckCancel", txn, client, canceller.wallet)


async def _check_cancel_faulty(
    accounts: dict[str, UserAccount],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return
    src = choice(list(accounts.values()))

    mutation = choice([
        "fake_check_id",
        "cancel_others_check",
    ])

    if mutation == "fake_check_id":
        txn = CheckCancel(
            account=src.address,
            check_id=params.fake_id(),
        )
    else:  # cancel_others_check — random account tries to cancel
        txn = CheckCancel(
            account=src.address,
            check_id=params.fake_id(),
        )

    await submit_tx("CheckCancel", txn, client, src.wallet)
