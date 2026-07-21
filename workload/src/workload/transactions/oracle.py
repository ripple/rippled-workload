"""Price Oracle transaction generators (XLS-47): OracleSet + OracleDelete."""

from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.models.transactions import OracleDelete, OracleSet
from xrpl.models.transactions.oracle_set import PriceData
from xrpl.wallet import Wallet

from workload import params
from workload.fuzz import submit_fuzzed
from workload.models import Oracle, UserAccount
from workload.randoms import choice, random
from workload.submit import submit_raw, submit_tx


def _price_data_series() -> list[PriceData]:
    """1-5 PriceData entries with unique, non-degenerate token pairs (rippled
    rejects base==quote and duplicate pairs with temMALFORMED)."""
    seen: set[tuple[str, str]] = set()
    entries: list[PriceData] = []
    for _ in range(params.oracle_price_data_count()):
        base = params.oracle_base_asset()
        quote = params.oracle_quote_asset()
        if base == quote or (base, quote) in seen:
            continue
        seen.add((base, quote))
        entries.append(
            PriceData(
                base_asset=base,
                quote_asset=quote,
                asset_price=params.oracle_asset_price(),
                scale=params.oracle_scale(),
            )
        )
    if not entries:  # every draw collided; guarantee a non-empty series
        entries.append(
            PriceData(
                base_asset="USD",
                quote_asset="EUR",
                asset_price=params.oracle_asset_price(),
                scale=params.oracle_scale(),
            )
        )
    return entries


# ── OracleSet ────────────────────────────────────────────────────────


async def oracle_set(
    accounts: dict[str, UserAccount], oracles: list[Oracle], client: AsyncJsonRpcClient
) -> None:
    if params.should_send_faulty():
        return await _oracle_set_faulty(accounts, oracles, client)
    return await _oracle_set_valid(accounts, oracles, client)


def _oracle_set_base(
    accounts: dict[str, UserAccount], oracles: list[Oracle]
) -> tuple[OracleSet, Wallet] | None:
    """Valid OracleSet + wallet. ~40% updates an existing oracle (keeping its
    immutable provider/asset_class), else creates a fresh one."""
    if not accounts:
        return None
    src = choice(list(accounts.values()))
    owned = [o for o in oracles if o.account == src.address]
    if owned and random() < 0.4:
        oracle = choice(owned)
        txn = OracleSet(
            account=src.address,
            oracle_document_id=oracle.document_id,
            provider=oracle.provider,
            asset_class=oracle.asset_class,
            last_update_time=params.oracle_last_update_time(),
            price_data_series=_price_data_series(),
        )
    else:
        txn = OracleSet(
            account=src.address,
            oracle_document_id=params.oracle_document_id(),
            provider=params.oracle_provider(),
            asset_class=params.oracle_asset_class(),
            last_update_time=params.oracle_last_update_time(),
            price_data_series=_price_data_series(),
        )
    return txn, src.wallet


async def _oracle_set_valid(
    accounts: dict[str, UserAccount], oracles: list[Oracle], client: AsyncJsonRpcClient
) -> None:
    built = _oracle_set_base(accounts, oracles)
    if built is None:
        return
    txn, wallet = built
    await submit_tx("OracleSet", txn, client, wallet)


async def _oracle_set_faulty(
    accounts: dict[str, UserAccount], oracles: list[Oracle], client: AsyncJsonRpcClient
) -> None:
    built = _oracle_set_base(accounts, oracles)
    if built is None:
        return
    base, wallet = built

    mutation = choice(
        ["fuzz", "stale_time", "empty_series", "oversize_series", "provider_mismatch"]
    )

    if mutation == "fuzz":
        await submit_fuzzed("OracleSet", base, client, wallet)
        return

    if mutation == "stale_time":
        # Centuries before the ledger close time -> tecINVALID_UPDATE_TIME.
        txn = base.__replace__(last_update_time=params.oracle_stale_update_time())
        await submit_tx("OracleSet", txn, client, wallet)
        return

    if mutation == "empty_series":
        # xrpl-py rejects an empty price_data_series at construction; empty it on
        # the wire so rippled preflight does -> temARRAY_EMPTY.
        def _empty(d: dict) -> None:
            d["PriceDataSeries"] = []

        await submit_raw("OracleSet", base, client, wallet, mutate=_empty)
        return

    if mutation == "oversize_series":
        # >10 entries -> preflight temARRAY_TOO_LARGE (size check precedes the
        # duplicate-pair check, so replicating one entry is enough).
        def _oversize(d: dict) -> None:
            series = d.get("PriceDataSeries") or []
            if series:
                d["PriceDataSeries"] = series * 11

        await submit_raw("OracleSet", base, client, wallet, mutate=_oversize)
        return

    # provider_mismatch: update an existing oracle with a different provider ->
    # inconsistent immutable field -> temMALFORMED.
    owned = [o for o in oracles if o.account in accounts]
    if not owned:
        return
    oracle = choice(owned)
    owner = accounts[oracle.account]
    txn = OracleSet(
        account=owner.address,
        oracle_document_id=oracle.document_id,
        provider=params.oracle_provider(),
        asset_class=oracle.asset_class,
        last_update_time=params.oracle_last_update_time(),
        price_data_series=_price_data_series(),
    )
    await submit_tx("OracleSet", txn, client, owner.wallet)


# ── OracleDelete ─────────────────────────────────────────────────────


async def oracle_delete(
    accounts: dict[str, UserAccount], oracles: list[Oracle], client: AsyncJsonRpcClient
) -> None:
    if params.should_send_faulty():
        return await _oracle_delete_faulty(accounts, oracles, client)
    return await _oracle_delete_valid(accounts, oracles, client)


def _oracle_delete_base(
    accounts: dict[str, UserAccount], oracles: list[Oracle]
) -> tuple[OracleDelete, Wallet] | None:
    """Valid OracleDelete (owner deletes one of its oracles) + wallet."""
    owned = [o for o in oracles if o.account in accounts]
    if not owned:
        return None
    oracle = choice(owned)
    owner = accounts[oracle.account]
    txn = OracleDelete(account=owner.address, oracle_document_id=oracle.document_id)
    return txn, owner.wallet


async def _oracle_delete_valid(
    accounts: dict[str, UserAccount], oracles: list[Oracle], client: AsyncJsonRpcClient
) -> None:
    built = _oracle_delete_base(accounts, oracles)
    if built is None:
        return
    txn, wallet = built
    await submit_tx("OracleDelete", txn, client, wallet)


async def _oracle_delete_faulty(
    accounts: dict[str, UserAccount], oracles: list[Oracle], client: AsyncJsonRpcClient
) -> None:
    if not accounts:
        return

    mutation = choice(["fuzz", "no_entry", "invalid_flags"])

    if mutation == "fuzz":
        built = _oracle_delete_base(accounts, oracles)
        if built is None:
            return
        base, wallet = built
        await submit_fuzzed("OracleDelete", base, client, wallet)
        return

    if mutation == "no_entry":
        # Delete an oracle the account never created -> tecNO_ENTRY.
        src = choice(list(accounts.values()))
        txn = OracleDelete(account=src.address, oracle_document_id=params.oracle_document_id())
        await submit_tx("OracleDelete", txn, client, src.wallet)
        return

    # invalid_flags: OracleDelete defines no flags, so any bit -> temINVALID_FLAG.
    src = choice(list(accounts.values()))
    txn = OracleDelete(
        account=src.address,
        oracle_document_id=params.oracle_document_id(),
        flags=0x80000000,
    )
    await submit_tx("OracleDelete", txn, client, src.wallet)
