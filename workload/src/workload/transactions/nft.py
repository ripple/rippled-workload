from xrpl.models.requests import AccountNFTs
from xrpl.models.transactions import (
    NFTokenMint,
    NFTokenMintFlag,
    NFTokenModify,
    NFTokenCreateOffer,
    NFTokenCreateOfferFlag,
    NFTokenCancelOffer,
    NFTokenAcceptOffer,
)
from xrpl.models.transactions.transaction import Memo
import base58
import json
from workload import logging
from workload.randoms import randrange, sample, randint, choice
from workload import params
from workload.submit import submit_tx
import struct
log = logging.getLogger(__name__)

ByteLength = int

ISSUER_LENGTH: ByteLength = 20
BUFFER_LENGTH: ByteLength = 32

class BufferLengthError(ValueError):
    pass

class IssuerLengthError(ValueError):
    pass

def nftoken_mint_txn(account, taxon, client):
    nftoken_mint_info = {
        "account": account.address,
        "nftoken_taxon": taxon,
        "flags":  NFTokenMintFlag.TF_TRUSTLINE,
    }

    log.debug("Minting: %s", nftoken_mint_info)

    memo_msg = "Some really cool info no doubt"
    memo = Memo(memo_data=memo_msg.encode("utf-8").hex())
    nftoken_mint_info["memos"] = [memo]

    nftoken_mint_txn = NFTokenMint.from_dict(nftoken_mint_info)
    return nftoken_mint_txn

def nftoken_mint_legacy(account, taxnon, client):
    # Legacy sync function — kept for reference, not wired to any endpoint
    nftoken_mint_txn_dict = nftoken_mint_txn(account, taxnon, client)
    log.debug("nftoken_mint_txn_dict: %s", nftoken_mint_txn_dict)
    return nftoken_mint_txn_dict

def account_nfts(account, client):
    account_nfts_dict = {
        "account": account.address,
    }
    account_nfts_response = client.request(AccountNFTs.from_dict(account_nfts_dict))
    log.debug("account_nfts_response: %s", account_nfts_response)
    return account_nfts_response.result["account_nfts"]

def encode_nft_id(
    flags: int,
    transfer_fee: int,
    issuer: bytes,
    taxon: int,
    token_seq: int,
) -> str:
    """Create a unique 256-bit NFTokenID.

    000B 0539 C35B55AA096BA6D87A6E6C965A6534150DC56E5E 12C5D09E 0000000C
    +--- +--- +--------------------------------------- +------- +-------
    |    |    |                                        |        |
    |    |    |                                        |        `---> Sequence: 12
    |    |    |                                        |
    |    |    |                                        `---> Scrambled Taxon: 314,953,886
    |    |    |                                              Unscrambled Taxon: 1337
    |    |    |
    |    |    `---> Issuer: rJoxBSzpXhPtAuqFmqxQtGKjA13jUJWthE
    |    |
    |    `---> TransferFee: 1337.0 bps or 13.37%
    |
    `---> Flags: 11 -> lsfBurnable, lsfOnlyXRP and lsfTransferable

    Returns:
        A 64-character hex string (256 bits).

    Raises:
        BufferLengthError: If buffer is not 32 bytes.
        IssuerLengthError: If issuer is no 20 bytes.

    """
    issuer_bytes = base58.b58decode_check(issuer, alphabet=base58.XRP_ALPHABET)
    issuer = issuer_bytes[1:] if len(issuer_bytes) == ISSUER_LENGTH + 1 else issuer_bytes
    if len(issuer) != ISSUER_LENGTH:
        msg = f"Issuer must be {ISSUER_LENGTH} bytes"
        raise IssuerLengthError(msg)

    taxon ^= (384160001 * token_seq + 2459) & 0xFFFFFFFF
    buf = b"".join(
        [
            struct.pack(">H", flags & 0xFFFF),
            struct.pack(">H", transfer_fee & 0xFFFF),
            issuer,
            struct.pack(">I", taxon & 0xFFFFFFFF),
            struct.pack(">I", token_seq & 0xFFFFFFFF),
        ],
    )

    if len(buf) != BUFFER_LENGTH:
        msg = f"Packed buffer is not {BUFFER_LENGTH} bytes!"
        raise BufferLengthError(msg)

    nftoken_id = buf.hex().upper()
    log.debug("NFT %s", locals())
    log.debug("Decoded %s", nftoken_id)
    return nftoken_id


# ── Mint (dispatch) ──────────────────────────────────────────────────

async def nftoken_mint(accounts, nfts, client):
    if params.should_send_faulty():
        return await _nftoken_mint_faulty(accounts, nfts, client)
    return await _nftoken_mint_valid(accounts, nfts, client)


async def _nftoken_mint_valid(accounts, nfts, client):
    account_id = choice(list(accounts))
    account = accounts[account_id]
    memo_msg = params.nft_memo()
    memo = Memo(memo_data=memo_msg.encode("utf-8").hex())
    txn = NFTokenMint(
        account=account.address,
        transfer_fee=params.nft_transfer_fee(),
        nftoken_taxon=params.nft_taxon(),
        flags=NFTokenMintFlag.TF_TRANSFERABLE,
        memos=[memo],
    )
    await submit_tx("NFTokenMint", txn, client, account.wallet)


async def _nftoken_mint_faulty(accounts, nfts, client):
    pass  # TODO: fault injection


# ── Burn ─────────────────────────────────────────────────────────────

async def nftoken_burn(accounts, nfts, client):
    if params.should_send_faulty():
        return await _nftoken_burn_faulty(accounts, nfts, client)
    return await _nftoken_burn_valid(accounts, nfts, client)


async def _nftoken_burn_valid(accounts, nfts, client):
    from xrpl.models.transactions import NFTokenBurn
    if not nfts:
        log.debug("No NFTs to burn")
        return
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
    if params.should_send_faulty():
        return await _nftoken_modify_faulty(accounts, client)
    return await _nftoken_modify_valid(accounts, nfts, client)


async def _nftoken_modify_valid(accounts, nfts, client):
    if not nfts:
        log.debug("No NFTs to modify")
        return
    from workload.randoms import choice
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


async def _nftoken_modify_faulty(accounts, client):
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
    # Randomly create sell or buy offer
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
        # Buy offer: a different account offers to buy
        other_accounts = [a for a in accounts if a != nft.owner]
        if not other_accounts:
            return
        buyer_id = choice(other_accounts)
        buyer = accounts[buyer_id]
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
    if params.should_send_faulty():
        return await _nftoken_cancel_offer_faulty(accounts, nft_offers, client)
    return await _nftoken_cancel_offer_valid(accounts, nft_offers, client)


async def _nftoken_cancel_offer_valid(accounts, nft_offers, client):
    if not nft_offers:
        log.debug("No NFT offers to cancel")
        return
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
    if params.should_send_faulty():
        return await _nftoken_accept_offer_faulty(accounts, nfts, nft_offers, client)
    return await _nftoken_accept_offer_valid(accounts, nfts, nft_offers, client)


async def _nftoken_accept_offer_valid(accounts, nfts, nft_offers, client):
    if not nft_offers:
        log.debug("No NFT offers to accept")
        return
    offer = choice(nft_offers)
    if offer.is_sell:
        # A different account accepts the sell offer
        other_accounts = [a for a in accounts if a != offer.creator]
        if not other_accounts:
            return
        buyer_id = choice(other_accounts)
        buyer = accounts[buyer_id]
        txn = NFTokenAcceptOffer(
            account=buyer.address,
            nftoken_sell_offer=offer.offer_id,
        )
        wallet = buyer.wallet
    else:
        # The NFT owner accepts the buy offer
        # Find the NFT to get the owner
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
