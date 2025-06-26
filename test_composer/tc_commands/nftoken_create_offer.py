import asyncio
import json
from pathlib import Path

import accounting
from client import rippled, workload_json
from xrpl.asyncio.transaction import submit_and_wait
from xrpl.models.transactions import NFTokenCreateOffer, NFTokenCreateOfferFlag

from workload import logger
from workload.randoms import choice

data = json.loads(Path(workload_json).read_text(encoding="utf-8"))

create_amount = "1000000"


async def nftoken_create_offer(account, nft_id, wallet):
    logger.info("Creating offer for %s's nft [%s]", account, nft_id)
    nftoken_offer_create_txn = NFTokenCreateOffer(
        account=account,
        nftoken_id=nft_id,
        amount=create_amount,
        flags=NFTokenCreateOfferFlag.TF_SELL_NFTOKEN,
    )
    logger.debug(json.dumps(nftoken_offer_create_txn.to_xrpl(), indent=2))

    nftoken_offer_create_txn_response = await submit_and_wait(transaction=nftoken_offer_create_txn, client=rippled, wallet=wallet)

    return nftoken_offer_create_txn_response.result


owners = accounting.get_nft_owners()

if owners:
    logger.debug(f"NFT_owners: {owners}")
    owner = choice(owners)
    logger.debug(f"owner: {owner}")
    nfts = accounting.get_nfts(owner)
    logger.debug(f"nfts: {nfts}")
    nftid = choice(accounting.get_nfts(owner))
    logger.debug(f"{owner}-{nftid}")
    wallet = accounting.load_wallet_from_account(owner)
    create_offer_response = asyncio.run(nftoken_create_offer(owner, nftid, wallet))
    offer_id = create_offer_response["meta"]["offer_id"]
    # if debug/verbose
    # for node in create_offer_response["meta"]["AffectedNodes"]:
    #     created_node = node.get("CreatedNode")
    #     if created_node and created_node.get("LedgerEntryType") in {"NFTokenOffer", "DirectoryNode"}:
    #         logger.info(json.dumps(created_node, indent=2))
    accounting.log_nft_offer_create(owner, offer_id, "sell")

else:
    logger.info("No accounts have minted NFTs")
