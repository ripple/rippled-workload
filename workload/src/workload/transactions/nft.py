"""NFToken transaction generators for the antithesis workload."""

from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.models.transactions import (
    NFTokenAcceptOffer,
    NFTokenBurn,
    NFTokenCancelOffer,
    NFTokenCreateOffer,
    NFTokenCreateOfferFlag,
    NFTokenMint,
    NFTokenMintFlag,
    NFTokenModify,
)
from xrpl.models.transactions.transaction import Memo
from xrpl.wallet import Wallet

from workload import logging, params
from workload.fuzz import submit_fuzzed
from workload.models import NFT, NFTOffer, UserAccount
from workload.randoms import choice, random
from workload.submit import submit_raw, submit_tx

log = logging.getLogger(__name__)


# ── Mint ─────────────────────────────────────────────────────────────


async def nftoken_mint(
    accounts: dict[str, UserAccount], nfts: list[NFT], client: AsyncJsonRpcClient
) -> None:
    if params.should_send_faulty():
        return await _nftoken_mint_faulty(accounts, nfts, client)
    return await _nftoken_mint_valid(accounts, nfts, client)


def _nftoken_mint_base(
    accounts: dict[str, UserAccount],
) -> tuple[NFTokenMint, Wallet] | None:
    """Valid NFTokenMint + wallet; shared by valid and fuzz."""
    if not accounts:
        return None
    account = choice(list(accounts.values()))
    # ~30% mutable so NFTokenModify has targets
    flags = NFTokenMintFlag.TF_TRANSFERABLE
    if random() < 0.30:
        flags |= NFTokenMintFlag.TF_MUTABLE
    txn = NFTokenMint(
        account=account.address,
        transfer_fee=params.nft_transfer_fee(),
        nftoken_taxon=params.nft_taxon(),
        flags=flags,
        memos=[Memo(memo_data=params.nft_memo().encode("utf-8").hex())],
    )
    return txn, account.wallet


async def _nftoken_mint_valid(
    accounts: dict[str, UserAccount], nfts: list[NFT], client: AsyncJsonRpcClient
) -> None:
    built = _nftoken_mint_base(accounts)
    if built is None:
        return
    txn, wallet = built
    await submit_tx("NFTokenMint", txn, client, wallet)


async def _nftoken_mint_faulty(
    accounts: dict[str, UserAccount], nfts: list[NFT], client: AsyncJsonRpcClient
) -> None:
    if len(accounts) < 2:
        return
    acct_list = list(accounts.values())

    if choice(["foreign_issuer", "fuzz"]) == "fuzz":
        built = _nftoken_mint_base(accounts)
        if built is None:
            return
        base, wallet = built
        await submit_fuzzed("NFTokenMint", base, client, wallet)
        return

    # issuer hasn't authorized src as minter → tecNO_PERMISSION
    src = choice(acct_list)
    other = choice([a for a in acct_list if a.address != src.address])
    txn = NFTokenMint(
        account=src.address,
        nftoken_taxon=params.nft_taxon(),
        issuer=other.address,
        flags=NFTokenMintFlag.TF_TRANSFERABLE,
    )
    await submit_tx("NFTokenMint", txn, client, src.wallet)


# ── Burn ─────────────────────────────────────────────────────────────


async def nftoken_burn(
    accounts: dict[str, UserAccount], nfts: list[NFT], client: AsyncJsonRpcClient
) -> None:
    if not nfts:
        return
    if params.should_send_faulty():
        return await _nftoken_burn_faulty(accounts, nfts, client)
    return await _nftoken_burn_valid(accounts, nfts, client)


def _nftoken_burn_base(
    accounts: dict[str, UserAccount], nfts: list[NFT]
) -> tuple[NFTokenBurn, Wallet] | None:
    """Valid NFTokenBurn of an owned NFT + wallet; shared by valid and fuzz."""
    nft = choice(nfts)
    if nft.owner not in accounts:
        return None
    owner = accounts[nft.owner]
    txn = NFTokenBurn(account=owner.address, nftoken_id=nft.nftoken_id)
    return txn, owner.wallet


async def _nftoken_burn_valid(
    accounts: dict[str, UserAccount], nfts: list[NFT], client: AsyncJsonRpcClient
) -> None:
    built = _nftoken_burn_base(accounts, nfts)
    if built is None:
        return
    txn, wallet = built
    await submit_tx("NFTokenBurn", txn, client, wallet)


async def _nftoken_burn_faulty(
    accounts: dict[str, UserAccount], nfts: list[NFT], client: AsyncJsonRpcClient
) -> None:
    if not accounts:
        return

    mutation = choice(["nonexistent", "non_owner", "fuzz"])
    if mutation == "fuzz":
        built = _nftoken_burn_base(accounts, nfts)
        if built is None:
            return
        base, wallet = built
        await submit_fuzzed("NFTokenBurn", base, client, wallet)
        return

    if mutation == "nonexistent":
        # Fake NFTokenID → tecNO_ENTRY.
        src = choice(list(accounts.values()))
        txn = NFTokenBurn(account=src.address, nftoken_id=params.fake_id())
        await submit_tx("NFTokenBurn", txn, client, src.wallet)
        return

    # non_owner: burn a tracked NFT the submitter doesn't own → tecNO_PERMISSION.
    nft = choice(nfts)
    non_owners = [a for a in accounts.values() if a.address != nft.owner]
    if not non_owners:
        return
    src = choice(non_owners)
    txn = NFTokenBurn(account=src.address, nftoken_id=nft.nftoken_id)
    await submit_tx("NFTokenBurn", txn, client, src.wallet)


# ── Modify ───────────────────────────────────────────────────────────


async def nftoken_modify(
    accounts: dict[str, UserAccount], nfts: list[NFT], client: AsyncJsonRpcClient
) -> None:
    if not nfts:
        return
    if params.should_send_faulty():
        return await _nftoken_modify_faulty(accounts, nfts, client)
    return await _nftoken_modify_valid(accounts, nfts, client)


def _nftoken_modify_base(
    accounts: dict[str, UserAccount], nfts: list[NFT]
) -> tuple[NFTokenModify, Wallet] | None:
    """Valid NFTokenModify of an owned NFT + wallet; shared by valid and fuzz."""
    nft = choice(nfts)
    if nft.owner not in accounts:
        return None
    owner = accounts[nft.owner]
    txn = NFTokenModify(
        account=owner.address,
        nftoken_id=nft.nftoken_id,
        uri=params.nft_uri(),
    )
    return txn, owner.wallet


async def _nftoken_modify_valid(
    accounts: dict[str, UserAccount], nfts: list[NFT], client: AsyncJsonRpcClient
) -> None:
    built = _nftoken_modify_base(accounts, nfts)
    if built is None:
        return
    txn, wallet = built
    await submit_tx("NFTokenModify", txn, client, wallet)


async def _nftoken_modify_faulty(
    accounts: dict[str, UserAccount], nfts: list[NFT], client: AsyncJsonRpcClient
) -> None:
    if not accounts:
        return

    mutation = choice(["nonexistent", "non_owner", "oversize_uri", "fuzz"])
    if mutation == "fuzz":
        built = _nftoken_modify_base(accounts, nfts)
        if built is None:
            return
        base, wallet = built
        await submit_fuzzed("NFTokenModify", base, client, wallet)
        return

    if mutation == "nonexistent":
        # Fake NFTokenID → tecNO_ENTRY.
        src = choice(list(accounts.values()))
        txn = NFTokenModify(
            account=src.address,
            nftoken_id=params.fake_id(),
            uri=params.nft_uri(),
        )
        await submit_tx("NFTokenModify", txn, client, src.wallet)
        return

    if mutation == "oversize_uri":
        # URI > 256 bytes → temMALFORMED (xrpl-py rejects at construction).
        built = _nftoken_modify_base(accounts, nfts)
        if built is None:
            return
        base, wallet = built

        def _mutate(d: dict) -> None:
            d["URI"] = "ab" * 300

        await submit_raw("NFTokenModify", base, client, wallet, _mutate)
        return

    # non_owner: modify a tracked NFT the submitter doesn't own → tecNO_PERMISSION.
    nft = choice(nfts)
    non_owners = [a for a in accounts.values() if a.address != nft.owner]
    if not non_owners:
        return
    src = choice(non_owners)
    txn = NFTokenModify(
        account=src.address,
        nftoken_id=nft.nftoken_id,
        uri=params.nft_uri(),
    )
    await submit_tx("NFTokenModify", txn, client, src.wallet)


# ── Create Offer ─────────────────────────────────────────────────────


async def nftoken_create_offer(
    accounts: dict[str, UserAccount],
    nfts: list[NFT],
    nft_offers: list[NFTOffer],
    client: AsyncJsonRpcClient,
) -> None:
    if not nfts:
        return
    if params.should_send_faulty():
        return await _nftoken_create_offer_faulty(accounts, nfts, nft_offers, client)
    return await _nftoken_create_offer_valid(accounts, nfts, nft_offers, client)


def _nftoken_create_offer_base(
    accounts: dict[str, UserAccount], nfts: list[NFT]
) -> tuple[NFTokenCreateOffer, Wallet] | None:
    """Valid NFTokenCreateOffer (sell or buy) + wallet; shared by valid and fuzz."""
    nft = choice(nfts)
    if nft.owner not in accounts:
        return None
    owner = accounts[nft.owner]
    if choice([True, False]):
        txn = NFTokenCreateOffer(
            account=owner.address,
            nftoken_id=nft.nftoken_id,
            amount=params.nft_offer_amount(),
            flags=NFTokenCreateOfferFlag.TF_SELL_NFTOKEN,
        )
        return txn, owner.wallet
    other_accounts = [a for a in accounts if a != nft.owner]
    if not other_accounts:
        return None
    buyer = accounts[choice(other_accounts)]
    txn = NFTokenCreateOffer(
        account=buyer.address,
        nftoken_id=nft.nftoken_id,
        amount=params.nft_offer_amount(),
        owner=nft.owner,
    )
    return txn, buyer.wallet


async def _nftoken_create_offer_valid(
    accounts: dict[str, UserAccount],
    nfts: list[NFT],
    nft_offers: list[NFTOffer],
    client: AsyncJsonRpcClient,
) -> None:
    built = _nftoken_create_offer_base(accounts, nfts)
    if built is None:
        return
    txn, wallet = built
    await submit_tx("NFTokenCreateOffer", txn, client, wallet)


async def _nftoken_create_offer_faulty(
    accounts: dict[str, UserAccount],
    nfts: list[NFT],
    nft_offers: list[NFTOffer],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return

    mutation = choice(["nonexistent", "sell_not_owned", "fuzz"])
    if mutation == "fuzz":
        built = _nftoken_create_offer_base(accounts, nfts)
        if built is None:
            return
        base, wallet = built
        await submit_fuzzed("NFTokenCreateOffer", base, client, wallet)
        return

    if mutation == "nonexistent":
        # Sell offer on a fake NFTokenID → tecNO_ENTRY.
        src = choice(list(accounts.values()))
        txn = NFTokenCreateOffer(
            account=src.address,
            nftoken_id=params.fake_id(),
            amount=params.nft_offer_amount(),
            flags=NFTokenCreateOfferFlag.TF_SELL_NFTOKEN,
        )
        await submit_tx("NFTokenCreateOffer", txn, client, src.wallet)
        return

    # sell_not_owned: sell offer on a tracked NFT the submitter doesn't own → tecNO_PERMISSION.
    nft = choice(nfts)
    non_owners = [a for a in accounts.values() if a.address != nft.owner]
    if not non_owners:
        return
    src = choice(non_owners)
    txn = NFTokenCreateOffer(
        account=src.address,
        nftoken_id=nft.nftoken_id,
        amount=params.nft_offer_amount(),
        flags=NFTokenCreateOfferFlag.TF_SELL_NFTOKEN,
    )
    await submit_tx("NFTokenCreateOffer", txn, client, src.wallet)


# ── Cancel Offer ─────────────────────────────────────────────────────


async def nftoken_cancel_offer(
    accounts: dict[str, UserAccount], nft_offers: list[NFTOffer], client: AsyncJsonRpcClient
) -> None:
    if not nft_offers:
        return
    if params.should_send_faulty():
        return await _nftoken_cancel_offer_faulty(accounts, nft_offers, client)
    return await _nftoken_cancel_offer_valid(accounts, nft_offers, client)


def _nftoken_cancel_offer_base(
    accounts: dict[str, UserAccount], nft_offers: list[NFTOffer]
) -> tuple[NFTokenCancelOffer, Wallet] | None:
    """Valid NFTokenCancelOffer of a tracked offer + wallet; shared by valid and fuzz."""
    offer = choice(nft_offers)
    if offer.creator not in accounts:
        return None
    creator = accounts[offer.creator]
    txn = NFTokenCancelOffer(
        account=creator.address,
        nftoken_offers=[offer.offer_id],
    )
    return txn, creator.wallet


async def _nftoken_cancel_offer_valid(
    accounts: dict[str, UserAccount], nft_offers: list[NFTOffer], client: AsyncJsonRpcClient
) -> None:
    built = _nftoken_cancel_offer_base(accounts, nft_offers)
    if built is None:
        return
    txn, wallet = built
    await submit_tx("NFTokenCancelOffer", txn, client, wallet)


async def _nftoken_cancel_offer_faulty(
    accounts: dict[str, UserAccount], nft_offers: list[NFTOffer], client: AsyncJsonRpcClient
) -> None:
    if not accounts:
        return

    if choice(["nonexistent", "fuzz"]) == "fuzz":
        built = _nftoken_cancel_offer_base(accounts, nft_offers)
        if built is None:
            return
        base, wallet = built
        await submit_fuzzed("NFTokenCancelOffer", base, client, wallet)
        return

    # Non-existent offer id no-ops → tesSUCCESS (cancel ignores unknown ids).
    src = choice(list(accounts.values()))
    txn = NFTokenCancelOffer(
        account=src.address,
        nftoken_offers=[params.fake_id()],
    )
    await submit_tx("NFTokenCancelOffer", txn, client, src.wallet)


# ── Accept Offer ─────────────────────────────────────────────────────


async def nftoken_accept_offer(
    accounts: dict[str, UserAccount],
    nfts: list[NFT],
    nft_offers: list[NFTOffer],
    client: AsyncJsonRpcClient,
) -> None:
    if not nft_offers:
        return
    if params.should_send_faulty():
        return await _nftoken_accept_offer_faulty(accounts, nfts, nft_offers, client)
    return await _nftoken_accept_offer_valid(accounts, nfts, nft_offers, client)


def _nftoken_accept_offer_base(
    accounts: dict[str, UserAccount], nfts: list[NFT], nft_offers: list[NFTOffer]
) -> tuple[NFTokenAcceptOffer, Wallet] | None:
    """Valid NFTokenAcceptOffer (counterparty of a tracked offer) + wallet."""
    offer = choice(nft_offers)
    if offer.is_sell:
        other_accounts = [a for a in accounts if a != offer.creator]
        if not other_accounts:
            return None
        buyer = accounts[choice(other_accounts)]
        txn = NFTokenAcceptOffer(
            account=buyer.address,
            nftoken_sell_offer=offer.offer_id,
        )
        return txn, buyer.wallet
    nft = next((n for n in nfts if n.nftoken_id == offer.nftoken_id), None)
    if not nft or nft.owner not in accounts:
        return None
    owner = accounts[nft.owner]
    txn = NFTokenAcceptOffer(
        account=owner.address,
        nftoken_buy_offer=offer.offer_id,
    )
    return txn, owner.wallet


async def _nftoken_accept_offer_valid(
    accounts: dict[str, UserAccount],
    nfts: list[NFT],
    nft_offers: list[NFTOffer],
    client: AsyncJsonRpcClient,
) -> None:
    built = _nftoken_accept_offer_base(accounts, nfts, nft_offers)
    if built is None:
        return
    txn, wallet = built
    await submit_tx("NFTokenAcceptOffer", txn, client, wallet)


async def _nftoken_accept_offer_faulty(
    accounts: dict[str, UserAccount],
    nfts: list[NFT],
    nft_offers: list[NFTOffer],
    client: AsyncJsonRpcClient,
) -> None:
    if not accounts:
        return

    mutation = choice(["nonexistent", "accept_own", "fuzz"])
    if mutation == "fuzz":
        built = _nftoken_accept_offer_base(accounts, nfts, nft_offers)
        if built is None:
            return
        base, wallet = built
        await submit_fuzzed("NFTokenAcceptOffer", base, client, wallet)
        return

    if mutation == "nonexistent":
        # Accept a fake sell offer → tecOBJECT_NOT_FOUND.
        src = choice(list(accounts.values()))
        txn = NFTokenAcceptOffer(
            account=src.address,
            nftoken_sell_offer=params.fake_id(),
        )
        await submit_tx("NFTokenAcceptOffer", txn, client, src.wallet)
        return

    # accept_own: creator accepts their own tracked offer → tecCANT_ACCEPT_OWN_NFTOKEN_OFFER.
    offer = choice(nft_offers)
    if offer.creator not in accounts:
        return
    creator = accounts[offer.creator]
    if offer.is_sell:
        txn = NFTokenAcceptOffer(
            account=creator.address,
            nftoken_sell_offer=offer.offer_id,
        )
    else:
        txn = NFTokenAcceptOffer(
            account=creator.address,
            nftoken_buy_offer=offer.offer_id,
        )
    await submit_tx("NFTokenAcceptOffer", txn, client, creator.wallet)
