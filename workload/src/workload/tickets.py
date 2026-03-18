"""Ticket transaction generators for the antithesis workload."""

import json

import xrpl.models
from workload import logging, params
from workload.assertions import tx_submitted, tx_result
from workload.randoms import choice
from xrpl.asyncio.account import get_next_valid_seq_number
from xrpl.asyncio.transaction import submit_and_wait
from xrpl.models.transactions import Payment

log = logging.getLogger(__name__)


# ── Create ───────────────────────────────────────────────────────────

async def ticket_create(accounts, client):
    if not accounts:
        return
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
    tx_submitted("TicketCreate", txn)
    response = await submit_and_wait(txn, client, account.wallet)
    result = response.result
    tx_result("TicketCreate", result)
    if result.get("engine_result") == "tesSUCCESS":
        ticket_seq = result["tx_json"]["Sequence"] + 1
        tix = list(range(ticket_seq, ticket_seq + ticket_count))
        account.tickets.update(tix)
        log.info("Created %d tickets for %s", ticket_count, account.address)


async def _ticket_create_faulty(accounts, client):
    pass  # TODO: fault injection


# ── Use ──────────────────────────────────────────────────────────────

async def ticket_use(accounts, client):
    if len(accounts) < 2:
        return
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
    tx_submitted("TicketUse", payment_txn)
    response = await submit_and_wait(payment_txn, client, src.wallet)
    tx_result("TicketUse", response.result)
    if response.result.get("engine_result") == "tesSUCCESS":
        src.tickets.discard(ticket_sequence)


async def _ticket_use_faulty(accounts, client):
    pass  # TODO: fault injection
