import json
import os
import pathlib
import sys

from client import txns_json, workload_json
from xrpl.clients import JsonRpcClient
from xrpl.transaction import sign_and_submit

from workload import logger
from workload.config import conf_file
from workload.create import generate_wallet_from_seed
from workload.offer import generate_offers

rippled_config = conf_file["workload"]["rippled"]

with open(workload_json, encoding="utf-8") as json_data:
    accounts_list = json.load(json_data)
    accounts = [generate_wallet_from_seed(a[1]) for a in accounts_list]

rippled_host = os.environ.get("RIPPLED_NAME") or rippled_config["local"]
rippled_rpc_port = os.environ.get("RIPPLED_RPC_PORT") or rippled_config["json_rpc_port"]
rippled = f"http://{rippled_host}:{rippled_rpc_port}"

client = JsonRpcClient(rippled)

NUM_OFFERS = 1

offer_responses = []
if __name__ == "__main__":
    if len(sys.argv) > 1:
        num_offers = int(sys.argv[1])
    for account in accounts:
        logger.debug("Creating offers for %s", account.address)
        offers = generate_offers(list(accounts), client, NUM_OFFERS)
        for wallet, txns in offers.items():
            for txn in txns:
                offer_responses.extend([sign_and_submit(txn, client, wallet)])

    with pathlib.Path(txns_json).open("w", encoding="utf-8") as txn_file:
        offer_data = []
        for o in offer_responses:
            tx_json = o.result["tx_json"]
            offer_data.append({
                "txn_hash": tx_json["hash"],
                "type": tx_json["TransactionType"],
                "account": tx_json["Account"],
                "sequence": tx_json["Sequence"],
            })
        json.dump(offer_data, txn_file, indent=2)
