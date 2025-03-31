from random import SystemRandom

from xrpl.models.requests import AccountNFTs
from xrpl.models.transactions import NFTokenMint, NFTokenMintFlag
from xrpl.models.transactions.transaction import Memo
from xrpl.transaction import autofill_and_sign, submit_and_wait

from workload import logging

urand = SystemRandom()
randrange = urand.randrange
sample = urand.sample

log = logging.getLogger(__name__)


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
