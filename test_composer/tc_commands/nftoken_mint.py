import asyncio
import json

import accounting
from client import generate_wallet_from_seed, rippled
from nft import encode_nft_id
import xrpl.constants
from xrpl.asyncio.account import get_next_valid_seq_number
from xrpl.asyncio.transaction import autofill_and_sign, submit_and_wait
from xrpl.models.requests import AccountObjects
from xrpl.models.transactions import NFTokenMint, NFTokenMintFlag
from xrpl.models.transactions.transaction import Memo

from workload import logger
from workload.models import UserAccount
from workload.randoms import choice


async def mint_nft(account):
    taxon = 0
    memo_msg = "Some really cool info no doubt"
    memo = Memo(memo_data=memo_msg.encode("utf-8").hex())
    nft_memo = [memo]
    flags = NFTokenMintFlag.TF_TRANSFERABLE
    transfer_fee = 666
    sequence = await get_next_valid_seq_number(account.address, rippled)
    nft_mint_txn = NFTokenMint(
        account=account.address,
        transfer_fee=transfer_fee,
        sequence=sequence,
        nftoken_taxon=taxon,
        flags=flags,
        memos=nft_memo,
        )
    logger.debug(json.dumps(nft_mint_txn.to_xrpl(), indent=2))
    logger.debug("Minting NFT %s", nft_mint_txn)
    nft_mint_txn_signed = await autofill_and_sign(transaction=nft_mint_txn, client=rippled, wallet=account.wallet)
    nft_id = encode_nft_id(flags, transfer_fee, account.address, taxon, sequence)
    accounting.log_nft(account, nft_id)
    nft_mint_txn_response = await submit_and_wait(transaction=nft_mint_txn_signed, client=rippled)
    if nft_mint_txn_response.status == "success":
        nftoken_id = nft_mint_txn_response.result["meta"]["nftoken_id"]
        msg = f"{account.address} minted NFT ID {nftoken_id}"
        logger.info(msg)
    return nft_mint_txn_response.result


def mint_random_nft():
    accounts = accounting.get_accounts()
    account = choice(accounts)
    wallet = generate_wallet_from_seed(account.split("-")[1])
    account = UserAccount(wallet=generate_wallet_from_seed(seed=wallet.seed))
    logger.debug("%s minting", account.address)
    txn_result = asyncio.run(mint_nft(account))
    logger.debug(json.dumps(txn_result, indent=2))


if __name__ == "__main__":
    mint_random_nft()
