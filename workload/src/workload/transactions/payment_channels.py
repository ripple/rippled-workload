"""PaymentChannelCreate / PaymentChannelFund / PaymentChannelClaim handlers.

A payment channel is a unidirectional XRP flow from source → destination.
The source pre-funds the channel, and the destination claims from it.
Either side can request closure subject to a settle delay.

State tracking keeps (channel_id, source, destination, amount, settle_delay)
so Fund and Claim can reference live channels.
"""

from __future__ import annotations

from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.models.transactions import (
    PaymentChannelClaim,
    PaymentChannelCreate,
    PaymentChannelFund,
)

from workload import params
from workload.models import PaymentChannel, UserAccount
from workload.randoms import choice, randint
from workload.submit import submit_tx




# ── PaymentChannelCreate ────────────────────────────────────────────


async def channel_create(
    accounts: dict[str, UserAccount],
    payment_channels: list[PaymentChannel],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _channel_create_faulty(accounts, client)
    return await _channel_create_valid(accounts, payment_channels, client)


async def _channel_create_valid(
    accounts: dict[str, UserAccount],
    payment_channels: list[PaymentChannel],
    client: AsyncJsonRpcClient,
) -> None:
    if len(accounts) < 2:
        return

    acct_list = list(accounts.values())
    src = choice(acct_list)
    # Destination must differ from source
    dst = choice([a for a in acct_list if a.address != src.address])

    amount = params.channel_amount()
    settle_delay = params.channel_settle_delay()

    txn = PaymentChannelCreate(
        account=src.address,
        destination=dst.address,
        amount=amount,
        settle_delay=settle_delay,
        public_key=src.wallet.public_key,
    )
    result = await submit_tx("PaymentChannelCreate", txn, client, src.wallet)

    # Optimistically track — real channel_id from state updater
    if result:
        tx_json = result.get("tx_json", result)
        tx_hash = tx_json.get("hash", "")
        if tx_hash:
            payment_channels.append(PaymentChannel(
                channel_id=tx_hash,
                source=src.address,
                destination=dst.address,
                amount=amount,
                settle_delay=settle_delay,
            ))


async def _channel_create_faulty(
    accounts: dict[str, UserAccount],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return
    src = choice(list(accounts.values()))

    mutation = choice([
        "zero_amount",
        "self_destination",
        "non_existent_destination",
        "past_cancel_after",
    ])

    if mutation == "zero_amount":
        txn = PaymentChannelCreate(
            account=src.address,
            destination=src.address,
            amount="0",
            settle_delay=60,
            public_key=src.wallet.public_key,
        )
    elif mutation == "self_destination":
        txn = PaymentChannelCreate(
            account=src.address,
            destination=src.address,
            amount=params.channel_amount(),
            settle_delay=params.channel_settle_delay(),
            public_key=src.wallet.public_key,
        )
    elif mutation == "non_existent_destination":
        txn = PaymentChannelCreate(
            account=src.address,
            destination=params.fake_account(),
            amount=params.channel_amount(),
            settle_delay=params.channel_settle_delay(),
            public_key=src.wallet.public_key,
        )
    else:  # past_cancel_after
        past = params._ripple_now() - randint(100, 10_000)
        txn = PaymentChannelCreate(
            account=src.address,
            destination=src.address,
            amount=params.channel_amount(),
            settle_delay=60,
            public_key=src.wallet.public_key,
            cancel_after=past,
        )

    await submit_tx("PaymentChannelCreate", txn, client, src.wallet)


# ── PaymentChannelFund ──────────────────────────────────────────────


async def channel_fund(
    accounts: dict[str, UserAccount],
    payment_channels: list[PaymentChannel],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _channel_fund_faulty(accounts, client)
    return await _channel_fund_valid(accounts, payment_channels, client)


async def _channel_fund_valid(
    accounts: dict[str, UserAccount],
    payment_channels: list[PaymentChannel],
    client: AsyncJsonRpcClient,
) -> None:
    if not payment_channels:
        return

    channel = choice(payment_channels)
    # Only the source can fund a channel
    src = accounts.get(channel.source)
    if not src:
        return

    add_amount = params.channel_fund_amount()

    txn = PaymentChannelFund(
        account=src.address,
        channel=channel.channel_id,
        amount=add_amount,
    )
    await submit_tx("PaymentChannelFund", txn, client, src.wallet)


async def _channel_fund_faulty(
    accounts: dict[str, UserAccount],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return
    src = choice(list(accounts.values()))

    mutation = choice([
        "fake_channel",
        "zero_amount",
        "non_owner_fund",
    ])

    if mutation == "fake_channel":
        txn = PaymentChannelFund(
            account=src.address,
            channel=params.fake_id(),
            amount=params.channel_fund_amount(),
        )
    elif mutation == "zero_amount":
        txn = PaymentChannelFund(
            account=src.address,
            channel=params.fake_id(),
            amount="0",
        )
    else:  # non_owner_fund — random account tries to fund
        txn = PaymentChannelFund(
            account=src.address,
            channel=params.fake_id(),
            amount=params.channel_fund_amount(),
        )

    await submit_tx("PaymentChannelFund", txn, client, src.wallet)


# ── PaymentChannelClaim ─────────────────────────────────────────────


async def channel_claim(
    accounts: dict[str, UserAccount],
    payment_channels: list[PaymentChannel],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _channel_claim_faulty(accounts, client)
    return await _channel_claim_valid(accounts, payment_channels, client)


async def _channel_claim_valid(
    accounts: dict[str, UserAccount],
    payment_channels: list[PaymentChannel],
    client: AsyncJsonRpcClient,
) -> None:
    if not payment_channels:
        return

    channel = choice(payment_channels)
    # Source or destination can submit a claim
    claimer_addr = choice([channel.source, channel.destination])
    claimer = accounts.get(claimer_addr)
    if not claimer:
        return

    txn = PaymentChannelClaim(
        account=claimer.address,
        channel=channel.channel_id,
    )
    await submit_tx("PaymentChannelClaim", txn, client, claimer.wallet)


async def _channel_claim_faulty(
    accounts: dict[str, UserAccount],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return
    src = choice(list(accounts.values()))

    mutation = choice([
        "fake_channel",
        "excessive_balance",
        "wrong_claimer",
    ])

    if mutation == "fake_channel":
        txn = PaymentChannelClaim(
            account=src.address,
            channel=params.fake_id(),
        )
    elif mutation == "excessive_balance":
        # Claim more than the channel holds
        txn = PaymentChannelClaim(
            account=src.address,
            channel=params.fake_id(),
            balance=str(randint(1_000_000_000, 10_000_000_000)),
            amount=str(randint(1_000_000_000, 10_000_000_000)),
        )
    else:  # wrong_claimer
        txn = PaymentChannelClaim(
            account=src.address,
            channel=params.fake_id(),
        )

    await submit_tx("PaymentChannelClaim", txn, client, src.wallet)

    await submit_tx("PaymentChannelCreate", txn, client, src.wallet)
