import asyncio
import sys

import accounting
from client import rippled
from xrpl.asyncio.transaction import submit_and_wait
from xrpl.models.transactions import NFTokenBurn

from workload import logger
from workload.randoms import choice


async def burn_nft(account, nft_id, wallet):
    nftburn_txn = NFTokenBurn(account=account,nftoken_id=nft_id)
    nftburn_txn_response = await submit_and_wait(transaction=nftburn_txn, client=rippled, wallet=wallet)
    return nftburn_txn_response.result


def burn_random_nft():
    nft_owners = accounting.get_nft_owners()
    if not nft_owners:
        logger.info("No accounts own NFTs")
        sys.exit(0)
    owner = choice(nft_owners)
    nftid = choice(accounting.get_nfts(owner))
    wallet = accounting.load_wallet_from_account(owner)
    x = asyncio.run(burn_nft(owner, nftid, wallet))
    affected_nodes = x["meta"]["AffectedNodes"]
    for node in affected_nodes:
        deleted_node = node.get("DeletedNode")
        if deleted_node and deleted_node.get("LedgerEntryType") == "DirectoryNode":
            deleted_nft_id = deleted_node.get("FinalFields", {}).get("NFTokenID")
            logger.info(f"Deleted NFT ID: {deleted_nft_id}")
    accounting.delete_nft(owner, nftid)


if __name__ == "__main__":
    burn_random_nft()
