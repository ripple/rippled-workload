import asyncio
import json

import accounting
from client import rippled
from xrpl.asyncio.transaction import submit_and_wait
from xrpl.models.transactions import NFTokenAcceptOffer

from workload import logger
from workload.create import generate_wallet_from_seed
from workload.randoms import choice


async def nftoken_accept_sell_offer(account, offer_id, wallet):

    nftoken_accept_txn = NFTokenAcceptOffer(
        account=account,
        nftoken_sell_offer=offer_id,
    )
    logger.debug(json.dumps(nftoken_accept_txn.to_xrpl(), indent=2))
    nftoken_accept_txn_response = await submit_and_wait(nftoken_accept_txn, rippled, wallet)
    logger.info("%s accepted sell offer for %s", account, offer_id)
    return nftoken_accept_txn_response.result


def accept_random_sell_offer():
    nft_sell_offers = accounting.get_nft_sell_offers()
    nft_sell_offer = choice(nft_sell_offers)
    seller, offer_id = nft_sell_offer.split("-")
    accounts = accounting.get_accounts()
    buyers = [a for a in accounts if a.split("-")[0] != seller]
    _, buyer_seed = choice(buyers).split("-")
    buyer = generate_wallet_from_seed(buyer_seed)
    sellers_nfts = asyncio.run(accounting.get_all_nfts(seller))

    logger.info(json.dumps(sellers_nfts.to_dict(), indent=2))

    result = asyncio.run(nftoken_accept_sell_offer(buyer.address, offer_id=offer_id, wallet=buyer))
    logger.info(json.dumps(result, indent=2))
    accounting.transfer_nft(seller, offer_id, buyer, result["meta"]["nftoken_id"])


if __name__ == "__main__":
    accept_random_sell_offer()
