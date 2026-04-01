"""AccountSet transaction generators for the antithesis workload.

Randomly sets/clears account flags that affect trust lines, payments,
and other operations.
"""

from workload import logging, params
from workload.randoms import choice, random
from workload.submit import submit_tx
from xrpl.models.transactions import AccountSet, AccountSetAsfFlag

log = logging.getLogger(__name__)

# Flags that are interesting for fuzzing trust lines and payments
INTERESTING_FLAGS = [
    AccountSetAsfFlag.ASF_DEFAULT_RIPPLE,
    AccountSetAsfFlag.ASF_REQUIRE_AUTH,
    AccountSetAsfFlag.ASF_REQUIRE_DEST,
    AccountSetAsfFlag.ASF_DISALLOW_XRP,
    AccountSetAsfFlag.ASF_GLOBAL_FREEZE,
    AccountSetAsfFlag.ASF_NO_FREEZE,
    AccountSetAsfFlag.ASF_DISABLE_INCOMING_TRUSTLINE,
    AccountSetAsfFlag.ASF_DISABLE_INCOMING_NFTOKEN_OFFER,
    AccountSetAsfFlag.ASF_DISABLE_INCOMING_PAYCHAN,
    AccountSetAsfFlag.ASF_DISABLE_INCOMING_CHECK,
]


async def account_set_random(accounts, client):
    if params.should_send_faulty():
        return await _account_set_faulty(accounts, client)
    return await _account_set_valid(accounts, client)


async def _account_set_valid(accounts, client):
    account_id = choice(list(accounts))
    account = accounts[account_id]
    flag = choice(INTERESTING_FLAGS)
    # Randomly set or clear the flag
    if random() < 0.5:
        txn = AccountSet(account=account.address, set_flag=flag)
    else:
        txn = AccountSet(account=account.address, clear_flag=flag)
    await submit_tx("AccountSet", txn, client, account.wallet)


async def _account_set_faulty(accounts, client):
    pass  # TODO: fault injection
