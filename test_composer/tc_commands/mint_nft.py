import asyncio
import json
import sys
import anyio
from client import generate_wallet_from_seed, rippled, workload_json
from xrpl.asyncio.account import get_next_valid_seq_number
from xrpl.asyncio.transaction import sign_and_submit, submit_and_wait
from xrpl.models.requests import AccountNFTs, AccountObjects
from xrpl.models.transactions import NFTokenCreateOffer, NFTokenCreateOfferFlag, NFTokenMint, NFTokenMintFlag
from xrpl.models.transactions.transaction import Memo

from workload import logger
from workload.models import UserAccount
from workload.randoms import choice


def load_accounts(account_data):
    return [UserAccount(wallet=generate_wallet_from_seed(seed=seed)) for acct, seed in account_data]

async def get_accounts():
    async with await anyio.open_file(workload_json) as json_data:
        data = await json_data.read()
        workload_data = json.loads(data)
    return load_accounts(workload_data["accounts"])

async def mint_nft(account):
    taxon = 0  # REVIEW: Maybe use this?
    memo_msg = "Some really cool info no doubt"
    memo = Memo(memo_data=memo_msg.encode("utf-8").hex())
    nft_memo = [memo]
    nft_mint_txn = NFTokenMint(
        account=account.address,
        nftoken_taxon=taxon,
        flags=NFTokenMintFlag.TF_TRANSFERABLE,
        memos=nft_memo,
        )
    logger.info("Minting NFT %s", nft_mint_txn)

    nft_mint_txn_response = await sign_and_submit(nft_mint_txn, rippled, account.wallet)
    return nft_mint_txn_response.result

async def get_nftoken_pages(wallet):
    # nftoken_pages = await rippled.request(AccountObjects(account=wallet.address, type="NFTokenPage"))
    nftoken_pages = await rippled.request(AccountObjects(account=wallet.address))
    nftoken_pages = [ao for ao in nftoken_pages.result["account_objects"] if ao["LedgerEntryType"] == "NFTokenPage"]
    # for i in nftoken_pages:
    #     print(f"{i=}")
    # sys.exit(0)
    return nftoken_pages
    ao = asyncio.run(get_account_object(account_id))

accounts = asyncio.run(get_accounts())
account = accounts[0]
res = asyncio.run(mint_nft(account))
nftoken_pages = asyncio.run(get_nftoken_pages(account))
nftp = nftoken_pages[0]

nftokens = nftp["NFTokens"]
nftoken_ids = [nft["NFToken"]["NFTokenID"] for nft in nftokens]

nftoken_id = nftoken_ids[0]
amount = "1000000"
flag = NFTokenCreateOfferFlag.TF_SELL_NFTOKEN

nftoken_offer_payload = {
      "TransactionType": "NFTokenCreateOffer",
      "Account": account.address,
      "NFTokenID": nftoken_id,
      "Amount": amount,
      "Flags": flag,
}

offer_payload = {
    "method": "submit",
    "params": [{
        "secret": account.wallet.seed,
        "tx_json": nftoken_offer_payload,
    }]
}
sequence = asyncio.run(get_next_valid_seq_number(account.address, rippled))

rippled
# nft_mint_txn_response = await sign_and_submit(nft_mint_txn, rippled, account.wallet)

print(f"Next sequence {sequence}")
nftco = NFTokenCreateOffer.from_xrpl(nftoken_offer_payload)
from xrpl.transaction import sign_and_submit, submit_and_wait
from xrpl.clients import JsonRpcClient
rippled = JsonRpcClient("http://atrippled:5005")
response = submit_and_wait(nftco, rippled, account.wallet)
