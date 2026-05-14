"""Price Oracle transaction generators for the antithesis workload (XLS-47)."""

from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.models.transactions import OracleDelete, OracleSet
from xrpl.models.transactions.oracle_set import PriceData

from workload import params
from workload.models import Oracle, UserAccount
from workload.randoms import choice, randint, sample
from workload.submit import submit_tx


def _random_price_data_series() -> list[PriceData]:
    """Generate a random list of 1-5 PriceData entries with unique pairs."""
    count = params.oracle_price_data_count()
    seen_pairs: set[tuple[str, str]] = set()
    entries: list[PriceData] = []
    for _ in range(count):
        base = params.oracle_base_asset()
        quote = params.oracle_quote_asset()
        # Ensure unique base/quote pairs within the series
        if (base, quote) in seen_pairs:
            continue
        seen_pairs.add((base, quote))
        entries.append(
            PriceData(
                base_asset=base,
                quote_asset=quote,
                asset_price=params.oracle_asset_price(),
                scale=params.oracle_scale(),
            )
        )
    # Must have at least one entry
    if not entries:
        entries.append(
            PriceData(
                base_asset=params.oracle_base_asset(),
                quote_asset=params.oracle_quote_asset(),
                asset_price=params.oracle_asset_price(),
                scale=params.oracle_scale(),
            )
        )
    return entries


# ── OracleSet ────────────────────────────────────────────────────────


async def oracle_set(
    accounts: dict[str, UserAccount],
    oracles: list[Oracle],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _oracle_set_faulty(accounts, oracles, client)
    return await _oracle_set_valid(accounts, oracles, client)


async def _oracle_set_valid(
    accounts: dict[str, UserAccount],
    oracles: list[Oracle],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return
    src = choice(list(accounts.values()))

    # 60% create new oracle, 40% update existing (if any exist for this account)
    account_oracles = [o for o in oracles if o.account == src.address]
    if account_oracles and randint(0, 99) < 40:
        # Update existing oracle — keep provider and asset_class the same
        oracle = choice(account_oracles)
        txn = OracleSet(
            account=src.address,
            oracle_document_id=oracle.document_id,
            provider=oracle.provider,
            asset_class=oracle.asset_class,
            last_update_time=params.oracle_last_update_time(),
            price_data_series=_random_price_data_series(),
        )
    else:
        # Create new oracle
        txn = OracleSet(
            account=src.address,
            oracle_document_id=params.oracle_document_id(),
            provider=params.oracle_provider(),
            asset_class=params.oracle_asset_class(),
            last_update_time=params.oracle_last_update_time(),
            price_data_series=_random_price_data_series(),
        )
    await submit_tx("OracleSet", txn, client, src.wallet)


async def _oracle_set_faulty(
    accounts: dict[str, UserAccount],
    oracles: list[Oracle],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return
    mutation = choice(
        [
            "non_owner_submission",
            "invalid_flags",
            "invalid_update_time",
            "provider_mismatch",
        ]
    )
    accounts_list = list(accounts.values())

    if mutation == "non_owner_submission":
        if len(accounts_list) < 2:
            return
        owner, impostor = sample(accounts_list, 2)
        txn = OracleSet(
            account=owner.address,
            oracle_document_id=params.oracle_document_id(),
            provider=params.oracle_provider(),
            asset_class=params.oracle_asset_class(),
            last_update_time=params.oracle_last_update_time(),
            price_data_series=_random_price_data_series(),
        )
        await submit_tx("OracleSet", txn, client, impostor.wallet)

    elif mutation == "invalid_flags":
        src = choice(accounts_list)
        txn = OracleSet(
            account=src.address,
            oracle_document_id=params.oracle_document_id(),
            provider=params.oracle_provider(),
            asset_class=params.oracle_asset_class(),
            last_update_time=params.oracle_last_update_time(),
            price_data_series=_random_price_data_series(),
            flags=0x80000000,
        )
        await submit_tx("OracleSet", txn, client, src.wallet)

    elif mutation == "invalid_update_time":
        src = choice(accounts_list)
        # Use a time far in the past but after the XRPL epoch to bypass
        # client validation — should trigger tecINVALID_UPDATE_TIME
        stale_time = 946684800 + 1  # just after XRPL epoch, ~year 2000
        txn = OracleSet(
            account=src.address,
            oracle_document_id=params.oracle_document_id(),
            provider=params.oracle_provider(),
            asset_class=params.oracle_asset_class(),
            last_update_time=stale_time,
            price_data_series=_random_price_data_series(),
        )
        await submit_tx("OracleSet", txn, client, src.wallet)

    elif mutation == "provider_mismatch":
        # Try to update an existing oracle with a different provider
        if not oracles:
            return
        oracle = choice(oracles)
        if oracle.account not in accounts:
            return
        owner = accounts[oracle.account]
        # Generate a different provider than the one stored
        wrong_provider = params.oracle_provider()
        txn = OracleSet(
            account=owner.address,
            oracle_document_id=oracle.document_id,
            provider=wrong_provider,
            asset_class=oracle.asset_class,
            last_update_time=params.oracle_last_update_time(),
            price_data_series=_random_price_data_series(),
        )
        await submit_tx("OracleSet", txn, client, owner.wallet)


# ── OracleDelete ─────────────────────────────────────────────────────


async def oracle_delete(
    accounts: dict[str, UserAccount],
    oracles: list[Oracle],
    client: AsyncJsonRpcClient,
) -> None:
    if params.should_send_faulty():
        return await _oracle_delete_faulty(accounts, oracles, client)
    return await _oracle_delete_valid(accounts, oracles, client)


async def _oracle_delete_valid(
    accounts: dict[str, UserAccount],
    oracles: list[Oracle],
    client: AsyncJsonRpcClient,
) -> None:
    if not oracles:
        return
    target = choice(oracles)
    if target.account not in accounts:
        return
    owner = accounts[target.account]
    txn = OracleDelete(
        account=owner.address,
        oracle_document_id=target.document_id,
    )
    await submit_tx("OracleDelete", txn, client, owner.wallet)


async def _oracle_delete_faulty(
    accounts: dict[str, UserAccount],
    oracles: list[Oracle],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return
    mutation = choice(
        [
            "delete_nonexistent",
            "non_owner_submission",
            "invalid_flags",
        ]
    )
    accounts_list = list(accounts.values())

    if mutation == "delete_nonexistent":
        src = choice(accounts_list)
        txn = OracleDelete(
            account=src.address,
            oracle_document_id=params.oracle_document_id(),
        )
        await submit_tx("OracleDelete", txn, client, src.wallet)

    elif mutation == "non_owner_submission":
        if not oracles or len(accounts_list) < 2:
            return
        target = choice(oracles)
        if target.account not in accounts:
            return
        impostors = [a for a in accounts_list if a.address != target.account]
        if not impostors:
            return
        impostor = choice(impostors)
        txn = OracleDelete(
            account=target.account,
            oracle_document_id=target.document_id,
        )
        await submit_tx("OracleDelete", txn, client, impostor.wallet)

    elif mutation == "invalid_flags":
        src = choice(accounts_list)
        txn = OracleDelete(
            account=src.address,
            oracle_document_id=params.oracle_document_id(),
            flags=0x80000000,
        )
        await submit_tx("OracleDelete", txn, client, src.wallet)
