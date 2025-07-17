import json
import os
import pathlib
import sys

import anyio
from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.constants import CryptoAlgorithm
from xrpl.core import keypairs
from xrpl.models.currencies import IssuedCurrency
from xrpl.wallet import Wallet

from workload import logger
from workload.config import conf_file
from workload.models import UserAccount

rippled_config = conf_file["workload"]["rippled"]
rippled_host = os.environ.get("RIPPLED_NAME") or rippled_config["local"]
rippled_rpc_port = os.environ.get("RIPPLED_RPC_PORT") or rippled_config["json_rpc_port"]
rippled_ = f"http://{rippled_host}:{rippled_rpc_port}"

rippled = AsyncJsonRpcClient(rippled_)

# States of the workload
workload_json_file = "workload.json"
docker_path = f"/opt/antithesis/{workload_json_file}"
local_workload_json_path = pathlib.Path(pathlib.Path(__file__).parent / workload_json_file)
workload_json = docker_path if os.environ.get("RIPPLED_NAME") else local_workload_json_path

txns_file = "txns.json"
docker_path = f"/opt/antithesis/{txns_file}"
local_txns_file_path = pathlib.Path(pathlib.Path(__file__).parent / txns_file)
txns_json = docker_path if os.environ.get("RIPPLED_NAME") else local_txns_file_path

default_algo = CryptoAlgorithm[conf_file["workload"]["accounts"]["default_crypto_algorithm"]]


async def load_workload_data(workload_json):
    logger.debug("Loading workload data at %s", workload_json)
    async with await anyio.open_file(workload_json) as json_data:
        data = await json_data.read()
        return json.loads(data)


async def load_txn_data(txns_json):
    if pathlib.Path(txns_json).is_file():
        logger.debug("Loading transaction data from %s", txns_json)
        async with await anyio.open_file(txns_json) as json_data:
            data = await json_data.read()
            return json.loads(data)
    logger.debug("No transactions file found! No transactions have been submitted yet?")
    sys.exit(0)


async def get_issued_currencies():
    wl_data = await load_workload_data(workload_json)
    return [IssuedCurrency(issuer=issuer, currency=currency) for currency, issuer in wl_data["issued_currencies"]]


async def get_currencies():
    wl_data = await load_workload_data(workload_json)
    return [{"issuer": issuer, "currency": currency} for currency, issuer in wl_data["issued_currencies"]]


async def get_accounts():
    wl_data = await load_workload_data(workload_json)
    return wl_data["accounts"]


async def get_txns():
    txns = await load_txn_data(txns_json)
    return txns


def generate_wallet_from_seed(seed: str, algorithm: CryptoAlgorithm = default_algo) -> Wallet:
    wallet = Wallet.from_seed(seed=seed, algorithm=algorithm)
    return wallet


def generate_wallet(algorithm: CryptoAlgorithm = default_algo):
    seed = keypairs.generate_seed(algorithm=algorithm)
    wallet = generate_wallet_from_seed(seed=seed, algorithm=algorithm)
    return wallet


async def get_wallets():
    async with await anyio.open_file(workload_json) as json_data:
        data = await json_data.read()
        workload_data = json.loads(data)
        wallets = [UserAccount(wallet=generate_wallet_from_seed(seed=seed)) for account, seed in workload_data]
    return wallets
