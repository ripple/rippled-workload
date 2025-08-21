from dataclasses import dataclass
from pprint import pprint
from typing import Callable, Dict, Any, Optional

from xrpl.account import get_next_valid_seq_number
from xrpl.models import IssuedCurrency
from xrpl.models.transactions.transaction import Memo
from xrpl.transaction import submit_and_wait
from xrpl.models.transactions import (
    AccountSetAsfFlag,
    Payment,
    NFTokenMint,
    NFTokenMintFlag,
    AccountSet,
    TrustSet,
)
from workload import randoms
from workload.create import generate_wallet_from_seed
from workload.randoms import sample, choice

def deep_update(base: dict, override: dict) -> dict:
    """Deep-merge override into base and return base."""
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_update(base[k], v)
        else:
            base[k] = v
    return base

def rand_amount(rng: randoms.SystemRandom) -> str:
    # XRP drops, string per XRPL JSON conventions
    return str(randoms.randint(10, 1_000_000))

def rand_address(account_list) -> str:
    return randoms.choice(account_list)

@dataclass
class TxnContext:
    account: str
    fee: str = "10"
    limit_amount_value = "10000000000000"
    addresses: Optional[list] = None
    def get_address(self) -> randoms.SystemRandom:
        return choice(self.addresses)

def build_payment(ctx: TxnContext) -> dict:
    address = ctx.get_address()

    return {
        "TransactionType": "Payment",
        "Account": ctx.account,
        "Fee": ctx.fee,
        "Destination": ctx.get_address(),
    }

def build_trustset(ctx: TxnContext) -> dict:
    return {
        "TransactionType": "TrustSet",
        "Account": ctx.account,
        "Fee": ctx.fee,
    }

def build_accountset(ctx: TxnContext) -> dict:
    rng = ctx.get_address()
    # TODO: Use ASFlags
    return {
        "Account": ctx.account,
        "Fee": ctx.fee,
        "SetFlag": randoms.choice(list(AccountSetAsfFlag)),
        # "Domain": ""
    }

def build_nftoken_mint(ctx: TxnContext) -> dict:
    address = ctx.get_address()
    memo_msg = "Some really cool info no doubt"
    memo = Memo(memo_data=memo_msg.encode("utf-8").hex())
    taxon = 0
    nftoken_mint_dict = {
        "Account": ctx.account,
        "NFTokenTaxon": taxon,
        "flags": NFTokenMintFlag.TF_TRANSFERABLE,
        "memos": [memo],
    }
    return nftoken_mint_dict

_BUILDERS: Dict[str, Callable[[TxnContext], dict]] = {
    "Payment": build_payment,
    "TrustSet": build_trustset,
    "AccountSet": build_accountset,
    "NFTokenMint": build_nftoken_mint,
}

TXN_FACTORY = {
    "Payment": Payment,
    "TrustSet": TrustSet,
    "AccountSet": AccountSet,
    "NFTokenMint": NFTokenMint,
}

def generate_txn(
    txn_type: str,
    ctx: TxnContext,
    **overrides: Any,
) -> dict:
    """
    Build a transaction of the given type using randomized defaults and deep-merge any overrides.
    """
    try:
        builder = _BUILDERS[txn_type]
    except KeyError:
        raise ValueError(f"Unsupported txn_type: {txn_type!r}") from None

    base = builder(ctx)
    updated_txn = deep_update(base, overrides)
    try:
        new_txn = TXN_FACTORY[txn_type].from_xrpl(updated_txn)
        return new_txn
    except Exception as e:
        print(f"error: {e}")
