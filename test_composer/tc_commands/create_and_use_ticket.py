import asyncio
import json
import time
from pathlib import Path

import xrpl
import xrpl.asyncio
from client import rippled, workload_json
from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.asyncio.transaction import submit_and_wait
from xrpl.models.requests import AccountObjects, AccountObjectType
from xrpl.wallet import Wallet

from workload import logger
from workload.create import create_accounts
from workload.randoms import choice, randint

wallets, _ = create_accounts(1, rippled)
time.sleep(3)
wallet = wallets.pop()
ticket_sequence = int
rippled = AsyncJsonRpcClient("http://atrippled:5005")

amount = "1000000"


async def get_next_sequence(address, client):
    return await xrpl.asyncio.account.get_next_valid_seq_number(address, client)


async def get_tickets(address):
    return await rippled.request(AccountObjects(account=address, type=AccountObjectType.TICKET))


async def get_ticket_sequences(address):
    tickets_result = await get_tickets(address)
    return sorted([o["TicketSequence"] for o in tickets_result.result["account_objects"]])


async def create_tickets(wallet: Wallet, ticket_count=5):
    ticket_create_txn = xrpl.models.TicketCreate(
        account=wallet.address,
        ticket_count=ticket_count,
    )
    return await submit_and_wait(ticket_create_txn, rippled, wallet)


async def use_ticket(wallet: Wallet, ticket_sequence: ticket_sequence):
    payment_txn = xrpl.models.Payment(
        account=wallet.address,
        destination=destination,
        amount=amount,
        sequence=0,
        ticket_sequence=ticket_sequence,
    )
    logger.info(f"Using ticket {ticket_sequence}")
    return await submit_and_wait(payment_txn, rippled, wallet)


accounts = json.loads(Path(workload_json).read_text(encoding="utf-8"))["accounts"]

destination, _ = accounts[randint(0, len(accounts)) - 1]

create_tickets_response = asyncio.run(create_tickets(wallet))
ticket_sequences = asyncio.run(get_ticket_sequences(wallet.address))
logger.info(f"{ticket_sequences=}")

asyncio.run(use_ticket(wallet, ticket_sequence=choice(ticket_sequences)))

ticket_sequences_after = asyncio.run(get_ticket_sequences(wallet.address))
logger.info(f"{ticket_sequences_after=}")
