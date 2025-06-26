import json
import os
from time import sleep

from accounting import log_account
from antithesis.assertions import always
from client import workload_json
from xrpl.account import does_account_exist
from xrpl.clients import JsonRpcClient

from workload import logger
from workload.config import conf_file
from workload.create import create_accounts

rippled_config = conf_file["workload"]["rippled"]
num_accounts = 2
with open(workload_json, encoding="utf-8") as json_data:
    accounts = json.load(json_data)

rippled_host = os.environ.get("RIPPLED_NAME") or rippled_config["local"]
rippled_rpc_port = os.environ.get("RIPPLED_RPC_PORT") or rippled_config["json_rpc_port"]
rippled = f"http://{rippled_host}:{rippled_rpc_port}"

client = JsonRpcClient(rippled)

wallets, responses = create_accounts(num_accounts, client)
sleep(6)

for wallet in wallets:
    address = wallet.address
    if account_exists := does_account_exist(address=address, client=client):
        logger.info("Created %s", address[0:8])
        log_account(wallet)
    always(condition=account_exists, message="Account created", details={"account_id": address})
