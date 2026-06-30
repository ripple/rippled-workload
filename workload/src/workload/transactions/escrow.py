"""EscrowCreate / EscrowFinish / EscrowCancel workload handlers."""

from __future__ import annotations

from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.models.transactions import EscrowCancel, EscrowCreate, EscrowFinish
from xrpl.wallet import Wallet

from workload import params
from workload.fuzz import submit_fuzzed
from workload.models import Escrow, UserAccount
from workload.randoms import choice, randint
from workload.submit import submit_tx

# ── EscrowCreate ────────────────────────────────────────────────────


async def escrow_create(
    accounts: dict[str, UserAccount],
    escrows: list[Escrow],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _escrow_create_faulty(accounts, client)
    return await _escrow_create_valid(accounts, escrows, client)


def _escrow_create_base(
    accounts: dict[str, UserAccount],
) -> tuple[EscrowCreate, Wallet, str | None] | None:
    """Valid EscrowCreate + wallet + fulfillment; shared by valid and fuzz.

    Fulfillment is one-way-derived from the condition and unrecoverable from the
    txn, so it rides alongside for the valid path's escrow tracking.
    """
    if not accounts:
        return None

    acct_list = list(accounts.values())
    src = choice(acct_list)
    dst = choice(acct_list)

    amount = params.escrow_amount()

    flavour = choice(["time_only", "condition_only", "time_and_condition"])

    condition = None
    fulfillment = None
    finish_after = None
    cancel_after = None

    if flavour == "time_only":
        finish_after = params.escrow_finish_after()
        cancel_after = params.escrow_cancel_after(finish_after)
    elif flavour == "condition_only":
        condition, fulfillment = params.escrow_condition_pair()
    else:  # time_and_condition
        finish_after = params.escrow_finish_after()
        cancel_after = params.escrow_cancel_after(finish_after)
        condition, fulfillment = params.escrow_condition_pair()

    txn = EscrowCreate(
        account=src.address,
        amount=amount,
        destination=dst.address,
        finish_after=finish_after,
        cancel_after=cancel_after,
        condition=condition,
    )
    return txn, src.wallet, fulfillment


async def _escrow_create_valid(
    accounts: dict[str, UserAccount],
    escrows: list[Escrow],
    client: AsyncJsonRpcClient,
) -> None:
    built = _escrow_create_base(accounts)
    if built is None:
        return
    txn, wallet, fulfillment = built
    result = await submit_tx("EscrowCreate", txn, client, wallet)

    # Track only when the submit will likely apply, else the entry leaks and
    # Finish/Cancel pick dead escrows.
    engine = result.get("engine_result", "") if result else ""
    if engine in ("tesSUCCESS", "terQUEUED", "terPRE_SEQ"):
        tx_json = result.get("tx_json", result)
        seq = tx_json.get("Sequence", 0)
        if seq:
            escrows.append(
                Escrow(
                    owner=txn.account,
                    destination=txn.destination,
                    sequence=seq,
                    condition=txn.condition,
                    fulfillment=fulfillment,
                    finish_after=txn.finish_after,
                    cancel_after=txn.cancel_after,
                )
            )


async def _escrow_create_faulty(
    accounts: dict[str, UserAccount],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return
    src = choice(list(accounts.values()))

    mutation = choice(
        [
            "fuzz",
            "past_cancel_after",
            "non_existent_destination",
            "bad_condition_hex",
            "no_time_no_condition",
        ]
    )
    if mutation == "fuzz":
        built = _escrow_create_base(accounts)
        if built is None:
            return
        base, wallet, _ = built
        await submit_fuzzed("EscrowCreate", base, client, wallet)
        return

    if mutation == "past_cancel_after":
        past = params._ripple_now() - randint(100, 10_000)
        txn = EscrowCreate(
            account=src.address,
            amount=params.escrow_amount(),
            destination=src.address,
            cancel_after=past,
        )
    elif mutation == "non_existent_destination":
        txn = EscrowCreate(
            account=src.address,
            amount=params.escrow_amount(),
            destination=params.fake_account(),
            cancel_after=params.escrow_finish_after() + 600,
        )
    elif mutation == "bad_condition_hex":
        # Valid hex but not a valid crypto-condition.
        bad_cond = params.fake_id()
        txn = EscrowCreate(
            account=src.address,
            amount=params.escrow_amount(),
            destination=src.address,
            condition=bad_cond,
            cancel_after=params.escrow_finish_after() + 600,
        )
    else:  # no_time_no_condition
        txn = EscrowCreate(
            account=src.address,
            amount=params.escrow_amount(),
            destination=src.address,
        )

    await submit_tx("EscrowCreate", txn, client, src.wallet)


# ── EscrowFinish ────────────────────────────────────────────────────


async def escrow_finish(
    accounts: dict[str, UserAccount],
    escrows: list[Escrow],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _escrow_finish_faulty(accounts, escrows, client)
    return await _escrow_finish_valid(accounts, escrows, client)


def _escrow_finish_base(
    accounts: dict[str, UserAccount],
    escrows: list[Escrow],
) -> tuple[EscrowFinish, Wallet] | None:
    """Valid EscrowFinish of a tracked escrow + wallet; shared by valid and fuzz."""
    if not escrows or not accounts:
        return None

    escrow = choice(escrows)
    # Anyone can finish an escrow.
    src = choice(list(accounts.values()))

    txn = EscrowFinish(
        account=src.address,
        owner=escrow.owner,
        offer_sequence=escrow.sequence,
        condition=escrow.condition,
        fulfillment=escrow.fulfillment,
    )
    return txn, src.wallet


async def _escrow_finish_valid(
    accounts: dict[str, UserAccount],
    escrows: list[Escrow],
    client: AsyncJsonRpcClient,
) -> None:
    built = _escrow_finish_base(accounts, escrows)
    if built is None:
        return
    txn, wallet = built
    await submit_tx("EscrowFinish", txn, client, wallet)


async def _escrow_finish_faulty(
    accounts: dict[str, UserAccount],
    escrows: list[Escrow],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return
    src = choice(list(accounts.values()))

    mutation = choice(
        [
            "fuzz",
            "non_existent_sequence",
            "wrong_fulfillment",
            "wrong_owner",
        ]
    )
    if mutation == "fuzz":
        built = _escrow_finish_base(accounts, escrows)
        if built is None:
            return
        base, wallet = built
        await submit_fuzzed("EscrowFinish", base, client, wallet)
        return

    if mutation == "non_existent_sequence":
        txn = EscrowFinish(
            account=src.address,
            owner=src.address,
            offer_sequence=randint(900_000, 999_999),
        )
    elif mutation == "wrong_fulfillment":
        # Two valid pairs, deliberately mismatched.
        cond, _ = params.escrow_condition_pair()
        _, wrong_ful = params.escrow_condition_pair()
        txn = EscrowFinish(
            account=src.address,
            owner=src.address,
            offer_sequence=randint(1, 100),
            condition=cond,
            fulfillment=wrong_ful,
        )
    else:  # wrong_owner
        txn = EscrowFinish(
            account=src.address,
            owner=params.fake_account(),
            offer_sequence=randint(1, 100),
        )

    await submit_tx("EscrowFinish", txn, client, src.wallet)


# ── EscrowCancel ────────────────────────────────────────────────────


async def escrow_cancel(
    accounts: dict[str, UserAccount],
    escrows: list[Escrow],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _escrow_cancel_faulty(accounts, escrows, client)
    return await _escrow_cancel_valid(accounts, escrows, client)


def _escrow_cancel_base(
    accounts: dict[str, UserAccount],
    escrows: list[Escrow],
) -> tuple[EscrowCancel, Wallet] | None:
    """Valid EscrowCancel of a cancellable escrow + wallet; shared by valid and fuzz."""
    if not escrows or not accounts:
        return None

    cancellable = [e for e in escrows if e.cancel_after is not None]
    if not cancellable:
        return None

    escrow = choice(cancellable)
    # Anyone can cancel an expired escrow.
    src = choice(list(accounts.values()))

    txn = EscrowCancel(
        account=src.address,
        owner=escrow.owner,
        offer_sequence=escrow.sequence,
    )
    return txn, src.wallet


async def _escrow_cancel_valid(
    accounts: dict[str, UserAccount],
    escrows: list[Escrow],
    client: AsyncJsonRpcClient,
) -> None:
    built = _escrow_cancel_base(accounts, escrows)
    if built is None:
        return
    txn, wallet = built
    await submit_tx("EscrowCancel", txn, client, wallet)


async def _escrow_cancel_faulty(
    accounts: dict[str, UserAccount],
    escrows: list[Escrow],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return
    src = choice(list(accounts.values()))

    mutation = choice(
        [
            "fuzz",
            "non_existent_sequence",
            "wrong_owner",
            "cancel_non_cancellable",
        ]
    )
    if mutation == "fuzz":
        built = _escrow_cancel_base(accounts, escrows)
        if built is None:
            return
        base, wallet = built
        await submit_fuzzed("EscrowCancel", base, client, wallet)
        return

    if mutation == "non_existent_sequence":
        txn = EscrowCancel(
            account=src.address,
            owner=src.address,
            offer_sequence=randint(900_000, 999_999),
        )
    elif mutation == "wrong_owner":
        txn = EscrowCancel(
            account=src.address,
            owner=params.fake_account(),
            offer_sequence=randint(1, 100),
        )
    else:  # cancel_non_cancellable
        txn = EscrowCancel(
            account=src.address,
            owner=src.address,
            offer_sequence=randint(1, 100),
        )

    await submit_tx("EscrowCancel", txn, client, src.wallet)
