import asyncio  # noqa: I001
import json
from pathlib import Path
import accounting
from accounting import load_wallet_from_account
from client import rippled, workload_json
from xrpl.asyncio.transaction import submit_and_wait
from xrpl.models.transactions import NFTokenBurn

from workload import logger
from workload.randoms import choice


async def burn_nft(account, nft_id, wallet):
    nftburn_txn = NFTokenBurn(
       account=account,
        nftoken_id=nft_id,
    )
    nftburn_txn_response = await submit_and_wait(nftburn_txn, rippled, wallet)

    return nftburn_txn_response.result


if nft_minters := accounting.get_nft_minters():
    minter = choice(nft_minters)
    nftid = choice(accounting.get_nfts(minter))
    logger.info(f"{minter=}")
    wallet = load_wallet_from_account(minter)
    x = asyncio.run(burn_nft(minter, nftid, wallet))
    affected_nodes = x["meta"]["AffectedNodes"]

    for an in affected_nodes:
        for k, v in an.items():
            if k == "DeletedNode" and v["LedgerEntryType"] == "DirectoryNode":
                deleted_nftid = v["FinalFields"]["NFTokenID"]
                logger.info(f"{nftid=}")
    accounting.delete_nft(minter, nftid )
else:
    logger.info("No accounts have minted NFTs")
