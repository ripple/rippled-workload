"""Batch transaction generators for the antithesis workload."""

import xrpl.models
from workload import logging, params
from workload.randoms import sample, choice
from workload.submit import submit_tx
from xrpl.asyncio.account import get_next_valid_seq_number
from xrpl.models import Batch, BatchFlag, Payment
from xrpl.models.transactions import AccountSet, AccountSetAsfFlag

log = logging.getLogger(__name__)

# Flags that work without extra fields.
# TODO: ASF_AUTHORIZED_NFTOKEN_MINTER excluded — requires nftoken_minter field.
#       Add back when we implement proper NFToken minter authorization with a real account from the pool.
_BATCH_SAFE_FLAGS = [
    f for f in AccountSetAsfFlag
    if f != AccountSetAsfFlag.ASF_AUTHORIZED_NFTOKEN_MINTER
]


async def batch_random(accounts, client):
    if len(accounts) < 2:
        return
    if params.should_send_faulty():
        return await _batch_random_faulty(accounts, client)
    return await _batch_random_valid(accounts, client)


async def _batch_random_valid(accounts, client):
    src_address, dst = sample(list(accounts), 2)
    sequence = await get_next_valid_seq_number(src_address, client)
    src = accounts[src_address]
    num_txns = params.batch_size()
    batch_flag = choice(list(BatchFlag))

    # Build inner transactions — randomly pick types
    inner_txns = []
    for idx in range(num_txns):
        inner_type = choice(["Payment", "AccountSet"])
        if inner_type == "Payment":
            inner = Payment(
                account=src.address,
                amount=params.batch_inner_amount(),
                flags=xrpl.models.TransactionFlag.TF_INNER_BATCH_TXN,
                destination=dst,
                sequence=sequence + idx + 1,
                fee="0",
                signing_pub_key="",
            )
        else:
            inner = AccountSet(
                account=src.address,
                set_flag=choice(_BATCH_SAFE_FLAGS),
                flags=xrpl.models.TransactionFlag.TF_INNER_BATCH_TXN,
                sequence=sequence + idx + 1,
                fee="0",
                signing_pub_key="",
            )
        inner_txns.append(inner)

    batch_txn = Batch(
        account=src.address,
        flags=batch_flag,
        raw_transactions=inner_txns,
        sequence=sequence,
    )
    await submit_tx("Batch", batch_txn, client, src.wallet)


async def _batch_random_faulty(accounts, client):
    pass  # TODO: fault injection
