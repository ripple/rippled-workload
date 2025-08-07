from random import SystemRandom

from xrpl.models.requests import AccountNFTs
from xrpl.models.transactions import NFTokenMint, NFTokenMintFlag
from xrpl.models.transactions.transaction import Memo
from xrpl.transaction import autofill_and_sign, submit_and_wait
import base58
from xrpl.asyncio.transaction import autofill_and_sign, submit_and_wait
import json
from workload import logging

urand = SystemRandom()
randrange = urand.randrange
sample = urand.sample
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
    taxon = 0
    memo_msg = "Some really cool info no doubt"
    memo = Memo(memo_data=memo_msg.encode("utf-8").hex())
    nft_memo = [memo]
    flags = NFTokenMintFlag.TF_TRANSFERABLE
    transfer_fee = 666
    nft_mint_txn = NFTokenMint(
        account=account.address,
        transfer_fee=transfer_fee,
        sequence=sequence,
        nftoken_taxon=taxon,
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
