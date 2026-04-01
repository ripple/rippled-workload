"""Ticket transaction generators for the antithesis workload."""

import xrpl.models
from workload import logging, params
from workload.randoms import choice
from workload.submit import submit_tx
from xrpl.models.transactions import Payment

log = logging.getLogger(__name__)


# ── Create ───────────────────────────────────────────────────────────

async def ticket_create(accounts, client):
    if params.should_send_faulty():
        return await _ticket_create_faulty(accounts, client)
    return await _ticket_create_valid(accounts, client)


async def _ticket_create_valid(accounts, client):
    account_id = choice(list(accounts))
    account = accounts[account_id]
    ticket_count = params.ticket_count()
    txn = xrpl.models.TicketCreate(
        account=account.address,
        ticket_count=ticket_count,
    )
    await submit_tx("TicketCreate", txn, client, account.wallet)


async def _ticket_create_faulty(accounts, client):
    pass  # TODO: fault injection


# ── Use ──────────────────────────────────────────────────────────────

async def ticket_use(accounts, client):
    if params.should_send_faulty():
        return await _ticket_use_faulty(accounts, client)
    return await _ticket_use_valid(accounts, client)


async def _ticket_use_valid(accounts, client):
    # Find an account with tickets
    accounts_with_tickets = [
        (addr, acc) for addr, acc in accounts.items() if acc.tickets
    ]
    if not accounts_with_tickets:
        log.debug("No accounts with tickets")
        return
    src_addr, src = choice(accounts_with_tickets)
    ticket_sequence = choice(list(src.tickets))
    # Pick a destination that isn't the source
    other_accounts = [a for a in accounts if a != src_addr]
    if not other_accounts:
        return
    dst = choice(other_accounts)
    payment_txn = Payment(
        account=src.address,
        destination=dst,
        amount=params.payment_amount(),
        sequence=0,
        ticket_sequence=ticket_sequence,
    )
    # Remove ticket optimistically to avoid reuse by concurrent calls
    src.tickets.discard(ticket_sequence)
    await submit_tx("TicketUse", payment_txn, client, src.wallet)


async def _ticket_use_faulty(accounts, client):
    pass  # TODO: fault injection
