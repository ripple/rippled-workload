"""Delegation transaction generators for the antithesis workload."""

from workload import logging, params
from workload.randoms import sample, randint
from workload.submit import submit_tx
from xrpl.models.transactions import DelegateSet
from xrpl.models.transactions.delegate_set import Permission, GranularPermission

log = logging.getLogger(__name__)


async def delegate_set(accounts, client):
    if params.should_send_faulty():
        return await _delegate_set_faulty(accounts, client)
    return await _delegate_set_valid(accounts, client)


async def _delegate_set_valid(accounts, client):
    src_id, delegate_id = sample(list(accounts), 2)
    src = accounts[src_id]
    all_perms = list(GranularPermission)
    num_perms = min(len(all_perms), randint(1, 3))
    selected = sample(all_perms, num_perms)
    permissions = [Permission(permission_value=p) for p in selected]
    txn = DelegateSet(
        account=src.address,
        authorize=delegate_id,
        permissions=permissions,
    )
    await submit_tx("DelegateSet", txn, client, src.wallet)


async def _delegate_set_faulty(accounts, client):
    pass  # TODO: fault injection
