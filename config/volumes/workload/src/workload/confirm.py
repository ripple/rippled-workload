import time
from xrpl.clients import JsonRpcClient
from xrpl.models import Tx

rippled_json_rpc = 'http://172.23.0.9:5005'

def confirm_account(txn_hash):
    jsonrpc_client = JsonRpcClient(rippled_json_rpc)
    tx_response = jsonrpc_client.request(Tx(transaction=txn_hash))
    return tx_response.result["validated"]

def confirm_accounts(txn_hashes):
    start = time.time()
    confirmed = []
    failed = []
    for txn_hash in txn_hashes:
        try:
            if confirm_account(txn_hash):
                confirmed.append(txn_hash)
        except Exception as e:
            failed.append(txn_hash)
            print(f"Couldn't confirm transaction {txn_hash}")
    end = time.time()
    elapsed = f"{(end-start):.6f}"
    print(f"Confirmed {len(txn_hashes)} accounts in {elapsed} seconds.")
    return confirmed, failed
