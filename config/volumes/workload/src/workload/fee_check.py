import asyncio
from xrpl.asyncio.clients import AsyncWebsocketClient
from workload import logging
from xrpl.asyncio.ledger import get_fee as get_fee_

log = logging.getLogger(__name__)

default_fee = 10
default_wait_time = 3
max_wait = 300

rippled_ws = 'ws://172.23.0.9:6005'

async def get_fee():
    async with AsyncWebsocketClient(rippled_ws) as client:
        return await get_fee_(client)

async def fee_is_high(normal_fee=default_fee):
    fee = await get_fee()
    if (fee_is_high := int(fee) > normal_fee):
        log.debug("Transaction fee (%s) higher than normal (%s)", fee, normal_fee)
    return fee_is_high

async def wait_for_normal_fee(wait_time=default_wait_time, wait_total=0):
    if not await fee_is_high() and wait_total:
        log.debug("Fee returned to normal after %s seconds." % wait_total)
    elif await fee_is_high():
        if await fee_is_high():
            if wait_total > max_wait:
                log.error(f"Exceeded max wait time! {max_wait}!")
                fee = await get_fee()
                raise Exception("Waited too long (%s>%s)s for fee (%s) to return to normal (%s)" % (wait_total, max_wait, fee, default_fee))
            if wait_total:
                log.debug("I've already waited %s seconds!" % wait_total)
            log.debug("Waiting %s seconds for fee to return to normal..." % wait_time)
            await asyncio.sleep(wait_time)
            await wait_for_normal_fee(wait_time * 2, wait_total + wait_time)
