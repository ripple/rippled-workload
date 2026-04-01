"""NFToken transaction generators for the antithesis workload."""

from workload import logging, params
from workload.randoms import choice
from workload.submit import submit_tx
from xrpl.models.transactions import (
    NFTokenMint, NFTokenMintFlag, NFTokenBurn, NFTokenModify,
    NFTokenCreateOffer, NFTokenCreateOfferFlag,
    NFTokenCancelOffer, NFTokenAcceptOffer,
)
from xrpl.models.transactions.transaction import Memo

log = logging.getLogger(__name__)


# ── Mint ─────────────────────────────────────────────────────────────

async def nftoken_mint(accounts, nfts, client):
    if params.should_send_faulty():
        return await _nftoken_mint_faulty(accounts, nfts, client)
    return await _nftoken_mint_valid(accounts, nfts, client)


async def _nftoken_mint_valid(accounts, nfts, client):
    account_id = choice(list(accounts))
    account = accounts[account_id]
    txn = NFTokenMint(
        account=account.address,
        transfer_fee=params.nft_transfer_fee(),
        nftoken_taxon=params.nft_taxon(),
        flags=NFTokenMintFlag.TF_TRANSFERABLE,
        memos=[Memo(memo_data=params.nft_memo().encode("utf-8").hex())],
    )
    await submit_tx("NFTokenMint", txn, client, account.wallet)


async def _nftoken_mint_faulty(accounts, nfts, client):
    pass  # TODO: fault injection


# ── Burn ─────────────────────────────────────────────────────────────

async def nftoken_burn(accounts, nfts, client):
    if not nfts:
        return
    if params.should_send_faulty():
        return await _nftoken_burn_faulty(accounts, nfts, client)
    return await _nftoken_burn_valid(accounts, nfts, client)


async def _nftoken_burn_valid(accounts, nfts, client):
    nft = choice(nfts)
    if nft.owner not in accounts:
        return
    owner = accounts[nft.owner]
    txn = NFTokenBurn(account=owner.address, nftoken_id=nft.nftoken_id)
    await submit_tx("NFTokenBurn", txn, client, owner.wallet)


async def _nftoken_burn_faulty(accounts, nfts, client):
    pass  # TODO: fault injection


# ── Modify ───────────────────────────────────────────────────────────

async def nftoken_modify(accounts, nfts, client):
    if not nfts:
        return
    if params.should_send_faulty():
        return await _nftoken_modify_faulty(accounts, nfts, client)
    return await _nftoken_modify_valid(accounts, nfts, client)


async def _nftoken_modify_valid(accounts, nfts, client):
    nft = choice(nfts)
    if nft.owner not in accounts:
        return
    owner = accounts[nft.owner]
    txn = NFTokenModify(
        account=owner.address,
        nftoken_id=nft.nftoken_id,
        uri=params.nft_uri(),
    )
    await submit_tx("NFTokenModify", txn, client, owner.wallet)


async def _nftoken_modify_faulty(accounts, nfts, client):
    pass  # TODO: fault injection


# ── Create Offer ─────────────────────────────────────────────────────

async def nftoken_create_offer(accounts, nfts, nft_offers, client):
    if not nfts:
        return
    if params.should_send_faulty():
        return await _nftoken_create_offer_faulty(accounts, nfts, nft_offers, client)
    return await _nftoken_create_offer_valid(accounts, nfts, nft_offers, client)


async def _nftoken_create_offer_valid(accounts, nfts, nft_offers, client):
    nft = choice(nfts)
    if nft.owner not in accounts:
        return
    owner = accounts[nft.owner]
    is_sell = choice([True, False])
    if is_sell:
        txn = NFTokenCreateOffer(
            account=owner.address,
            nftoken_id=nft.nftoken_id,
            amount=params.nft_offer_amount(),
            flags=NFTokenCreateOfferFlag.TF_SELL_NFTOKEN,
        )
        wallet = owner.wallet
    else:
        other_accounts = [a for a in accounts if a != nft.owner]
        if not other_accounts:
            return
        buyer = accounts[choice(other_accounts)]
        txn = NFTokenCreateOffer(
            account=buyer.address,
            nftoken_id=nft.nftoken_id,
            amount=params.nft_offer_amount(),
            owner=nft.owner,
        )
        wallet = buyer.wallet
    await submit_tx("NFTokenCreateOffer", txn, client, wallet)


async def _nftoken_create_offer_faulty(accounts, nfts, nft_offers, client):
    pass  # TODO: fault injection


# ── Cancel Offer ─────────────────────────────────────────────────────

async def nftoken_cancel_offer(accounts, nft_offers, client):
    if not nft_offers:
        return
    if params.should_send_faulty():
        return await _nftoken_cancel_offer_faulty(accounts, nft_offers, client)
    return await _nftoken_cancel_offer_valid(accounts, nft_offers, client)


async def _nftoken_cancel_offer_valid(accounts, nft_offers, client):
    offer = choice(nft_offers)
    if offer.creator not in accounts:
        return
    creator = accounts[offer.creator]
    txn = NFTokenCancelOffer(
        account=creator.address,
        nftoken_offers=[offer.offer_id],
    )
    await submit_tx("NFTokenCancelOffer", txn, client, creator.wallet)


async def _nftoken_cancel_offer_faulty(accounts, nft_offers, client):
    pass  # TODO: fault injection


# ── Accept Offer ─────────────────────────────────────────────────────

async def nftoken_accept_offer(accounts, nfts, nft_offers, client):
    if not nft_offers:
        return
    if params.should_send_faulty():
        return await _nftoken_accept_offer_faulty(accounts, nfts, nft_offers, client)
    return await _nftoken_accept_offer_valid(accounts, nfts, nft_offers, client)


async def _nftoken_accept_offer_valid(accounts, nfts, nft_offers, client):
    offer = choice(nft_offers)
    if offer.is_sell:
        other_accounts = [a for a in accounts if a != offer.creator]
        if not other_accounts:
            return
        buyer = accounts[choice(other_accounts)]
        txn = NFTokenAcceptOffer(
            account=buyer.address,
            nftoken_sell_offer=offer.offer_id,
        )
        wallet = buyer.wallet
    else:
        nft = next((n for n in nfts if n.nftoken_id == offer.nftoken_id), None)
        if not nft or nft.owner not in accounts:
            return
        owner = accounts[nft.owner]
        txn = NFTokenAcceptOffer(
            account=owner.address,
            nftoken_buy_offer=offer.offer_id,
        )
        wallet = owner.wallet
    await submit_tx("NFTokenAcceptOffer", txn, client, wallet)


async def _nftoken_accept_offer_faulty(accounts, nfts, nft_offers, client):
    pass  # TODO: fault injection
