#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = []
# ///
"""Prepare genesis ledger for an Antithesis experiment.

Takes a pre-generated genesis ledger (with funded accounts but empty amendments)
and injects all supported amendment hashes derived from rippled's features.macro.

Amendment hashes are SHA-512Half of the amendment name — no xrpld binary needed.

Usage:
    uv run prepare-workload/generate_genesis.py \
        --features-macro /path/to/features.macro \
        --genesis genesis/genesis_ledger.json \
        --accounts genesis/accounts.json \
        --output-dir testnet
"""

import argparse
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path


def sha512half(name: str) -> str:
    """Compute SHA-512Half of a string — the algorithm xrpld uses for amendment IDs."""
    return hashlib.sha512(name.encode()).digest()[:32].hex().upper()


def parse_features_macro(macro_path: Path) -> list[str]:
    """Extract supported amendment names from rippled's features.macro.

    Excludes retired amendments and comment examples.
    """
    text = "\n".join(line for line in macro_path.read_text().splitlines() if not line.lstrip().startswith("//"))
    amendments = []

    for m in re.finditer(r"XRPL_FEATURE\s*\(\s*(\w+)\s*,\s*Supported::yes\s*,", text):
        amendments.append(m.group(1))

    for m in re.finditer(r"XRPL_FIX\s*\(\s*(\w+)\s*,\s*Supported::yes\s*,", text):
        amendments.append("fix" + m.group(1))

    return sorted(amendments)


def main():
    parser = argparse.ArgumentParser(description="Inject amendments into a pre-generated genesis ledger")
    parser.add_argument(
        "--features-macro",
        required=True,
        type=Path,
        help="Path to rippled features.macro file",
    )
    parser.add_argument(
        "--genesis",
        type=Path,
        default=Path("genesis/genesis_ledger.json"),
        help="Path to pre-generated genesis ledger JSON",
    )
    parser.add_argument(
        "--accounts",
        type=Path,
        default=Path("genesis/accounts.json"),
        help="Path to pre-generated accounts JSON",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("testnet"),
        help="Output directory for prepared files",
    )
    args = parser.parse_args()

    for path, name in [
        (args.features_macro, "features.macro"),
        (args.genesis, "genesis ledger"),
        (args.accounts, "accounts"),
    ]:
        if not path.exists():
            sys.exit(f"{name} not found: {path}")

    # Parse amendment names and compute hashes
    amendment_names = parse_features_macro(args.features_macro)
    amendment_hashes = sorted(sha512half(name) for name in amendment_names)
    print(f"Computed {len(amendment_hashes)} amendment hashes from {args.features_macro.name}")

    # Load and patch the genesis ledger
    ledger = json.loads(args.genesis.read_text())
    for sle in ledger["ledger"]["accountState"]:
        if sle.get("LedgerEntryType") == "Amendments":
            sle["Amendments"] = amendment_hashes
            break
    else:
        sys.exit("No Amendments SLE found in genesis ledger")

    # Write output
    args.output_dir.mkdir(parents=True, exist_ok=True)

    ledger_out = args.output_dir / "genesis_ledger.json"
    ledger_out.write_text(json.dumps(ledger, indent=2))

    accounts_out = args.output_dir / "accounts.json"
    shutil.copy(args.accounts, accounts_out)

    accts = sum(1 for s in ledger["ledger"]["accountState"] if s.get("LedgerEntryType") == "AccountRoot")
    print(
        f"Output: {ledger_out} ({accts} accounts, {len(amendment_hashes)} amendments, {len(ledger['ledger']['accountState'])} SLEs)"
    )
    print(f"Output: {accounts_out}")


if __name__ == "__main__":
    main()
