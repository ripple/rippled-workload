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
from xrpl.transaction import autofill_and_sign, submit_and_wait
import base58
from xrpl.asyncio.transaction import autofill_and_sign, submit_and_wait
# from xrpl.transaction import autofill_and_sign
import json
from workload import logging
from workload.models import NFTOffer
from workload.randoms import randrange, sample, randint, choice
from workload import params
from workload.assertions import tx_submitted, tx_result
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
    try:
        signed_nftoken_mint_txn = autofill_and_sign(nftoken_mint_txn, client, account)
        log.debug("Signed NFTokenMint transaction: %s", signed_nftoken_mint_txn)
        return signed_nftoken_mint_txn
    except Exception:
        log.error("Couldn't sign transction!")

def nftoken_mint(account, taxnon, client):

    nftoken_mint_txn_dict = nftoken_mint_txn(account, taxnon, client)
    nftoken_mint_response = submit_and_wait(
        transaction=nftoken_mint_txn_dict,
        client=client,
        wallet=account,
        )
    log.debug("nftoken_mint_response: %s", nftoken_mint_response)
    return nftoken_mint_response

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
    logger.debug("NFT %s", locals())
    logger.debug("Decoded %s", nftoken_id)
    return nftoken_id

async def mint_nft(account, sequence, client):
    memo_msg = params.nft_memo()
    memo = Memo(memo_data=memo_msg.encode("utf-8").hex())
    nft_memo = [memo]
    flags = NFTokenMintFlag.TF_TRANSFERABLE
    transfer_fee = params.nft_transfer_fee()
    nft_mint_txn = NFTokenMint(
        account=account.address,
        transfer_fee=transfer_fee,
        sequence=sequence,
        nftoken_taxon=params.nft_taxon(),
        flags=flags,
        memos=nft_memo,
        )
    log.debug(json.dumps(nft_mint_txn.to_xrpl(), indent=2))
    log.debug("Minting NFT %s", nft_mint_txn)
    nft_mint_txn_signed = await autofill_and_sign(transaction=nft_mint_txn, client=client, wallet=account.wallet)
    # nft_id = encode_nft_id(flags, transfer_fee, account.address, taxon, sequence)
    nft_mint_txn_response = await submit_and_wait(transaction=nft_mint_txn_signed, client=client)
    if nft_mint_txn_response.status == "success":
        nftoken_id = nft_mint_txn_response.result["meta"]["nftoken_id"]
        msg = f"{account.address} minted NFT ID {nftoken_id}"
        log.info(msg)
    return nft_mint_txn_response.result


# ── Mint (dispatch) ──────────────────────────────────────────────────

async def nftoken_mint(accounts, nfts, client):
    if not accounts:
        return
    if params.should_send_faulty():
        return await _nftoken_mint_faulty(accounts, nfts, client)
    return await _nftoken_mint_valid(accounts, nfts, client)


async def _nftoken_mint_valid(accounts, nfts, client):
    from workload.models import NFT
    from xrpl.asyncio.account import get_next_valid_seq_number
    account_id = choice(list(accounts))
    account = accounts[account_id]
    sequence = await get_next_valid_seq_number(account.address, client)
    tx_submitted("NFTokenMint")
    result = await mint_nft(account, sequence, client)
    tx_result("NFTokenMint", result)
    if result.get("engine_result") == "tesSUCCESS":
        nftoken_id = result["meta"]["nftoken_id"]
        nfts.append(NFT(owner=account.address, nftoken_id=nftoken_id))
        account.nfts.add(nftoken_id)


async def _nftoken_mint_faulty(accounts, nfts, client):
    pass  # TODO: fault injection


# ── Burn ─────────────────────────────────────────────────────────────

async def nftoken_burn(accounts, nfts, client):
    if not accounts:
        return
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
    tx_submitted("NFTokenBurn")
    response = await submit_and_wait(transaction=txn, client=client, wallet=owner.wallet)
    tx_result("NFTokenBurn", response.result)
    if response.result.get("engine_result") == "tesSUCCESS":
        nfts.remove(nft)


async def _nftoken_burn_faulty(accounts, nfts, client):
    pass  # TODO: fault injection


# ── Modify ───────────────────────────────────────────────────────────

async def nftoken_modify(accounts, nfts, client):
    if not accounts:
        return
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
    tx_submitted("NFTokenModify")
    response = await submit_and_wait(transaction=txn, client=client, wallet=owner.wallet)
    tx_result("NFTokenModify", response.result)


async def _nftoken_modify_faulty(accounts, client):
    pass  # TODO: fault injection


# ── Create Offer ─────────────────────────────────────────────────────

async def nftoken_create_offer(accounts, nfts, nft_offers, client):
    if not accounts or not nfts:
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
    tx_submitted("NFTokenCreateOffer")
    response = await submit_and_wait(transaction=txn, client=client, wallet=wallet)
    result = response.result
    tx_result("NFTokenCreateOffer", result)
    if result.get("engine_result") == "tesSUCCESS":
        # Extract offer ID from metadata
        for node in result.get("meta", {}).get("AffectedNodes", []):
            created = node.get("CreatedNode", {})
            if created.get("LedgerEntryType") == "NFTokenOffer":
                offer_id = created.get("LedgerIndex", "")
                nft_offers.append(NFTOffer(
                    creator=txn.account,
                    offer_id=offer_id,
                    nftoken_id=nft.nftoken_id,
                    is_sell=is_sell,
                ))
                log.info("Created NFT %s offer %s", "sell" if is_sell else "buy", offer_id)
                break


async def _nftoken_create_offer_faulty(accounts, nfts, nft_offers, client):
    pass  # TODO: fault injection


# ── Cancel Offer ─────────────────────────────────────────────────────

async def nftoken_cancel_offer(accounts, nft_offers, client):
    if not accounts:
        return
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
    tx_submitted("NFTokenCancelOffer")
    response = await submit_and_wait(transaction=txn, client=client, wallet=creator.wallet)
    tx_result("NFTokenCancelOffer", response.result)
    if response.result.get("engine_result") == "tesSUCCESS":
        nft_offers.remove(offer)


async def _nftoken_cancel_offer_faulty(accounts, nft_offers, client):
    pass  # TODO: fault injection


# ── Accept Offer ─────────────────────────────────────────────────────

async def nftoken_accept_offer(accounts, nfts, nft_offers, client):
    if not accounts:
        return
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
    tx_submitted("NFTokenAcceptOffer")
    response = await submit_and_wait(transaction=txn, client=client, wallet=wallet)
    tx_result("NFTokenAcceptOffer", response.result)
    if response.result.get("engine_result") == "tesSUCCESS":
        nft_offers.remove(offer)
        # Update NFT ownership
        if offer.is_sell:
            for nft in nfts:
                if nft.nftoken_id == offer.nftoken_id:
                    nft.owner = txn.account
                    break


async def _nftoken_accept_offer_faulty(accounts, nfts, nft_offers, client):
    pass  # TODO: fault injection
