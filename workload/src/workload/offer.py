from xrpl.clients import JsonRpcClient
from xrpl.models.transactions import OfferCreate
from xrpl.wallet import Wallet
from workload.randoms import sample
from workload.utils import format_currency_short
from workload.randoms import gauss
from workload import logger
from workload.balances import get_account_tokens

def generate_offers(accounts: list[Wallet], client: JsonRpcClient, number_of_offers: int = 10) -> dict:
    """Generate random offers from tokens an account holds.

    Args:
        accounts (list[Wallet]): _Create offers for these accounts
        number_of_offers (int): How many offers to create

    Returns:
        _type_: list[OfferCreate]

    """
    STD_DEV = 1
    MEAN = 100
    NUM_ORDERS = 100
    def generate_prices(num_orders: int = NUM_ORDERS, mean: int = MEAN, std_dev: int = STD_DEV):
        round_to = 4
        data = [round(gauss(mean, std_dev), round_to) for _ in range(num_orders)]
        return data
    try:
        a = accounts.copy()
        logger.debug("Generating %s offers for each of", number_of_offers)
        for acc in a:
            logger.debug(acc.address)
        offers = {}
        while a: #next()?
            aa = a.pop()
            offers[aa] = []
            for _ in range(number_of_offers):
                all_account_tokens = list(get_account_tokens(aa, client)['held'].values())
                account_tokens = [a for at in all_account_tokens for a in at]
                bid_token, ask_token = sample(account_tokens, 2)  # Only try to create offers from tokens the account holds
                bid_price, ask_price = generate_prices(2, std_dev=5)
                bid, ask = (
                    bid_token.to_amount(bid_price),
                    ask_token.to_amount(ask_price),
                )
                offer = OfferCreate(account=aa.address, taker_gets=bid, taker_pays=ask)
                logger.debug("Created Offer bid %s ask %s", format_currency_short(bid), format_currency_short(ask))
                offers[aa].append(offer)
        logger.debug("Created offers: %s", offers)
        return offers
    except Exception as err:
        logger.exception("Couldn't generate list of offers\n%s")
        return {}
