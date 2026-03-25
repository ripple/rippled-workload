#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "httpx",
#     "xrpl-py",
# ]
# ///
"""Generate a genesis ledger JSON with pre-funded accounts and all amendments enabled.

Starts xrpld in standalone mode, queries it for all supported amendments,
funds N accounts from the genesis account, dumps the full ledger state,
and injects all amendment hashes into the Amendments SLE.

The output files are placed in the network config directory (--output-dir)
so they get baked into the Antithesis config image.

Usage:
    uv run prepare-workload/generate_genesis.py --xrpld /path/to/xrpld [--output-dir testnet]

Output:
    <output-dir>/genesis_ledger.json  — full ledger state for --ledgerfile
    <output-dir>/accounts.json        — account seeds for workload
"""

import argparse
import atexit
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from xrpl.constants import CryptoAlgorithm
from xrpl.models.transactions import Payment
from xrpl.transaction import sign as tx_sign
from xrpl.wallet import Wallet

RPC_PORT = 15005  # Non-standard port to avoid conflicts with running nodes
GENESIS_SEED = "snoPBrXtMeMyMHUVTgbuqAfg1SUTb"
NUM_ACCOUNTS = 100
FUND_AMOUNT = "100000000000"  # 100k XRP in drops
ALGO = CryptoAlgorithm.SECP256K1


def rpc(method: str, params: list | None = None) -> dict:
    r = httpx.post(
        f"http://127.0.0.1:{RPC_PORT}",
        json={"method": method, "params": params or [{}]},
        timeout=30,
    )
    return r.json()["result"]


def ledger_accept():
    return rpc("ledger_accept")


def generate_standalone_config(tmpdir: Path) -> Path:
    """Generate a minimal standalone xrpld config."""
    cfg = f"""\
[server]
port_rpc

[port_rpc]
port = {RPC_PORT}
ip = 127.0.0.1
admin = 127.0.0.1
protocol = http

[node_db]
type = NuDB
path = {tmpdir}/nudb

[database_path]
{tmpdir}

[node_size]
small

[signing_support]
true
"""
    cfg_path = tmpdir / "standalone.cfg"
    cfg_path.write_text(cfg)
    return cfg_path


def start_xrpld(xrpld_bin: Path, cfg_path: Path) -> subprocess.Popen:
    """Start xrpld standalone and wait for RPC to be ready."""
    proc = subprocess.Popen(
        [str(xrpld_bin), "--conf", str(cfg_path), "-a", "--start"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    atexit.register(lambda: (proc.terminate(), proc.wait()))

    for _ in range(30):
        try:
            rpc("server_info")
            return proc
        except Exception:
            time.sleep(0.5)

    out = proc.stdout.read(4096).decode() if proc.stdout else ""
    proc.terminate()
    raise RuntimeError(f"xrpld failed to start (cfg: {cfg_path})\n{out}")


def main():
    parser = argparse.ArgumentParser(description="Generate genesis ledger with pre-funded accounts")
    parser.add_argument("--xrpld", required=True, type=Path, help="Path to xrpld binary")
    parser.add_argument("--num-accounts", type=int, default=NUM_ACCOUNTS)
    parser.add_argument("--output-dir", type=Path, default=Path("testnet"))
    args = parser.parse_args()

    if not args.xrpld.exists():
        sys.exit(f"xrpld not found: {args.xrpld}")

    with tempfile.TemporaryDirectory(prefix="genesis_") as tmpdir:
        tmpdir = Path(tmpdir)
        cfg_path = generate_standalone_config(tmpdir)
        print(f"Starting xrpld standalone on :{RPC_PORT}...")
        proc = start_xrpld(args.xrpld, cfg_path)

        info = rpc("server_info")
        print(f"Connected: {info['info']['build_version']}")

        # Get all supported amendment hashes for injection into ledger
        feature_resp = rpc("feature")
        all_amendment_hashes = [
            h for h, info in feature_resp.get("features", {}).items()
            if isinstance(info, dict) and info.get("supported")
        ]
        print(f"Supported amendments: {len(all_amendment_hashes)}")

        for _ in range(5):
            ledger_accept()

        # Fund accounts from genesis
        genesis = Wallet.from_seed(GENESIS_SEED, algorithm=ALGO)
        acct_info = rpc("account_info", [{"account": genesis.address, "ledger_index": "current"}])
        seq = acct_info["account_data"]["Sequence"]

        accounts = []
        for i in range(args.num_accounts):
            w = Wallet.create(algorithm=ALGO)
            accounts.append({"address": w.address, "seed": w.seed})

            payment = Payment(
                account=genesis.address,
                destination=w.address,
                amount=FUND_AMOUNT,
                sequence=seq + i,
                fee="12",
            )
            signed_tx = tx_sign(payment, genesis)
            result = rpc("submit", [{"tx_blob": signed_tx.blob()}])
            engine = result.get("engine_result", "unknown")
            if engine != "tesSUCCESS":
                print(f"  WARNING: account {i} got {engine}")

            if (i + 1) % 10 == 0:
                ledger_accept()
                if (i + 1) % 50 == 0:
                    print(f"  Funded {i+1}/{args.num_accounts}")

        for _ in range(3):
            ledger_accept()

        # Verify a few accounts
        for acc in accounts[:3]:
            info = rpc("account_info", [{"account": acc["address"], "ledger_index": "validated"}])
            balance = info.get("account_data", {}).get("Balance", "0")
            print(f"  {acc['address']}: {int(balance)/1_000_000:.0f} XRP")

        # Dump full ledger and inject all amendments
        ledger = rpc("ledger", [{"ledger_index": "validated", "full": True, "accounts": True, "expand": True}])

        for sle in ledger["ledger"]["accountState"]:
            if sle.get("LedgerEntryType") == "Amendments":
                sle["Amendments"] = sorted(all_amendment_hashes)
                print(f"Injected {len(all_amendment_hashes)} amendments into ledger")
                break

        # Write output
        args.output_dir.mkdir(parents=True, exist_ok=True)

        ledger_path = args.output_dir / "genesis_ledger.json"
        with open(ledger_path, "w") as f:
            json.dump({"ledger": ledger["ledger"]}, f, indent=2)
        print(f"Saved {ledger_path} ({len(ledger['ledger']['accountState'])} SLEs)")

        accounts_path = args.output_dir / "accounts.json"
        with open(accounts_path, "w") as f:
            json.dump(accounts, f, indent=2)
        print(f"Saved {accounts_path} ({len(accounts)} accounts)")

        proc.terminate()
        proc.wait()


if __name__ == "__main__":
    main()
