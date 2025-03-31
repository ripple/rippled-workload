#!/usr/bin/env -S python3 -u

import json
import os
import pathlib
from time import sleep

from client import workload_json
from antithesis.assertions import always
from workload import logger
from workload.config import conf_file
from workload.create import create_accounts
from xrpl.account import does_account_exist
from xrpl.clients import JsonRpcClient

rippled_config = conf_file["workload"]["rippled"]

docker_path = "/opt/antithesis/test/v1/workload.json"
local_path = pathlib.Path(pathlib.Path(__file__).parent / "workload.json")
path = docker_path if os.environ.get("RIPPLED_NAME") else local_path


with open(workload_json, encoding="utf-8") as json_data:
    accounts = json.load(json_data)

rippled_host = os.environ.get("RIPPLED_NAME") or rippled_config["local"]
rippled_rpc_port = os.environ.get("RIPPLED_RPC_PORT") or rippled_config["json_rpc_port"]
rippled = f"http://{rippled_host}:{rippled_rpc_port}"

client = JsonRpcClient(rippled)

wallets, responses = create_accounts(5, client)
sleep(6)

for wallet in wallets:
    address = wallet.address
    account_exists = does_account_exist(address=address, client=client)
    logger.info("Account %s exists: %s", address[0:8], account_exists)
    always(condition=account_exists, message="Account created", details={"account_id": address})
