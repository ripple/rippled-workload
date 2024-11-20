
import datetime

from xrpl.models.transactions import EscrowCreate, EscrowFinish
from xrpl.utils import datetime_to_ripple_time
from xrpl.wallet import Wallet

from workload import logging

log = logging.getLogger(__name__)
"""
    {
        "Account": "rf1BiGeXwwQoi8Z2ueFYTEXSwuJYfV2Jpn",
        "TransactionType": "EscrowCreate",
        "Amount": "10000",
        "Destination": "rsA2LpzuawewSBQXkiju3YQTMzW13pAAdW",
        # "CancelAfter": 533257958,
        "FinishAfter": 533171558,
        "Condition": "A0258020E3B0C44298FC1C149AFBF4C8996FB92427AE41E4649B934CA495991B7852B855810100",
        "DestinationTag": 23480,
        "SourceTag": 11747
    }
"""


def escrow_create_txn(
                    account: Wallet,
                    destination: Wallet,
                    amount: str,
                    **kwargs: dict[str, str]) -> EscrowCreate:

    finish_after = datetime_to_ripple_time(datetime.datetime.now(tz=datetime.UTC))
    escrow_create_dictionary = {
        "account": account.address,
        "destination": destination.address,
        "amount": amount,
    }
    escrow_create_dictionary.update(kwargs)
    ect = EscrowCreate.from_dict(escrow_create_dictionary)

    return ect

def escrow_finish_txn(
                    owner: Wallet,
                    account: Wallet,
                    offer_sequence: int,
    ) -> EscrowFinish:

    escrow_finish_dictionary = {
        "owner": owner.address,
        "account": account.address,
        "offer_sequence": offer_sequence,
    }
    eft = EscrowFinish.from_dict(escrow_finish_dictionary)

    return eft
