import asyncio

from client import rippled, get_wallets

from workload import logger
from workload.randoms import sample, randint
from xrpl.asyncio.account import get_next_valid_seq_number, get_balance, get_account_root
from xrpl.asyncio.transaction import sign_and_submit
from xrpl.models.currencies import XRP
from xrpl.models.requests import AccountInfo

from xrpl.models.transactions import Payment
from xrpl.utils import drops_to_xrp, xrp_to_drops

min_payment = 100000 # 0.1 XRP

async def main():
    wallets = await get_wallets()
    account, destination = sample(wallets, 2)
    logger.debug("Source account %s", account.address)
    logger.debug("Destination account %s", destination.address)
    max_payment = await get_balance(account.address, rippled)
    sequence = await get_next_valid_seq_number(account.address, rippled)
    amount = XRP().to_amount(value=drops_to_xrp(str(randint(min_payment, max_payment))))
    payment_txn = Payment(account=account.address, amount=amount, destination=destination.address, sequence=sequence)
    response = await sign_and_submit(payment_txn, rippled, account.wallet)
    logger.debug("Payment from %s to %s for %s submitted.", account.address, destination.address, amount)
    return response, account.address, destination.address, amount

response, account, destination, amount = asyncio.run(main())
result = response.result
payment_data = account, destination, f"{drops_to_xrp(amount):,}"
if result["applied"]:
    logger.debug("Payment result %s", result["engine_result"])
    logger.info("Successful payment from %s to %s for %s.", *payment_data )
else:
    logger.error("Payment %s failed!", *payment_data)

