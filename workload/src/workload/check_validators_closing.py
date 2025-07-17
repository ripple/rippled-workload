import os
import sys
import time
import json
import asyncio
import logging
import httpx
from typing import Optional

logger = logging.getLogger(__name__)
stdout_log_formatter = logging.Formatter(
    '%(name)s: %(asctime)s | %(levelname)s | %(filename)s:%(lineno)s | %(process)d | %(message)s'
)

stdout_log_handler = logging.StreamHandler(stream=sys.stdout)
stdout_log_handler.setLevel(logging.INFO)
stdout_log_handler.setFormatter(stdout_log_formatter)

logger.addHandler(stdout_log_handler)
logger.setLevel(logging.INFO)

REQUEST_TIMEOUT = 5  # seconds
NUM_VALIDATORS = 5
VALIDATOR_NAME = "atval"


async def get_latest_validated_ledger_sequence(client: httpx.AsyncClient) -> int:
    response = await client.post("/", json={"method": "ledger", "params": [{"ledger_index": "validated"}]})
    response.raise_for_status()
    return int(response.json()["result"]["ledger"]["ledger_index"])


async def wait_for_ledger_close(client: httpx.AsyncClient) -> None:
    target = await get_latest_validated_ledger_sequence(client) + 1
    while True:
        current = await get_latest_validated_ledger_sequence(client)
        if current >= target:
            logger.info("Arrived at ledger %s", target)
            return
        logger.info("At %s, waiting for ledger %s", current, target)
        await asyncio.sleep(3)


async def check_single_validator(vnum: int, base_name: str) -> bool:
    url = f"http://{base_name}{vnum}:5005"
    logger.info("Checking validator %s", vnum)
    async with httpx.AsyncClient(base_url=url, timeout=REQUEST_TIMEOUT) as client:
        try:
            response = await client.post("/", json={"method": "server_info"})
            response.raise_for_status()
        except httpx.ConnectTimeout:
            logger.error("Timeout: rippled didn't respond at %s", url)
            return False
        except httpx.ConnectError:
            logger.error("Connection error: No server seen at %s", url)
            sys.exit(1)
        except httpx.HTTPStatusError as e:
            logger.error("HTTP error from %s: %s", url, e)
            return False

        info = response.json()["result"]["info"]
        logger.info("server_info (validator %s): %s", vnum, json.dumps(info, indent=2))

        if info.get("server_state") != "proposing":
            logger.info("Validator %s not proposing", vnum)
            return False

        try:
            await wait_for_ledger_close(client)
            logger.info("Validator %s is closing ledgers.", vnum)
            return True
        except Exception as e:
            logger.error("Ledger wait failed for validator %s: %s", vnum, e)
            return False


async def check_validator_proposing(val_to_check: Optional[int] = None) -> bool:
    base_name = VALIDATOR_NAME or os.environ.get("VALIDATOR_NAME")
    num_validators = NUM_VALIDATORS or int(os.environ["NUM_VALIDATORS"])

    if val_to_check and val_to_check > num_validators:
        logger.error("Validator [%s] outside number of validators range [%s]", val_to_check, num_validators)
        return False

    val_range = range(val_to_check - 1, val_to_check) if val_to_check else range(num_validators)
    validator_numbers = [v + 1 for v in val_range]

    tasks = [check_single_validator(vnum, base_name) for vnum in validator_numbers]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for vnum, result in zip(validator_numbers, results):
        if isinstance(result, Exception):
            logger.error("Validator %s failed with exception: %s", vnum, result)
            return False
        if not result:
            logger.warning("Validator %s not proposing or closing ledgers", vnum)
            return False

    return True


if __name__ == "__main__":
    import asyncio
    result = asyncio.run(check_validator_proposing())
    print("All good!" if result else "Some validator(s) not proposing.")
