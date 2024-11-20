from xrpl.models.amounts import IssuedCurrencyAmount
from xrpl.models.currencies import IssuedCurrency
from xrpl.models.transactions import OfferCreate

def quick_setup():
    pass  # TODO: implement

def short_address(address: str) -> str:
    length = 5
    sep = "..."
    return f"{address[:length]}{sep}{address[-length:]}"


def format_currency(currency: IssuedCurrency | IssuedCurrencyAmount | str | dict[str, str]) -> str:
    if isinstance(currency, str):  # TODO: Fix this
        xrp_balance = currency
        w, d = pieces if len(pieces := xrp_balance.split(".")) > 1 else (0, int(pieces[0]))
        if w := (float(d) // 1e6):  # It's at least 1 XRP
            if len(str(w)) > 2:
                xrp_balance = round(float(xrp_balance))
            elif len(str(w)) < 3:
                xrp_balance = f"{float(xrp_balance):.4g}"
            currency_str = f"{xrp_balance} XRP"
        else:
            currency_str = f"{d} drops"
    else:
        if isinstance(currency, dict):
            value = currency.get("value")
            issuer = currency.get("issuer")
            currency_str = currency.get("currency")
        elif type(currency) in {IssuedCurrency, IssuedCurrencyAmount}:
            value = currency.value if isinstance(currency, IssuedCurrencyAmount) else None
            issuer = currency.issuer
            currency_str = currency.currency
        try:
            if issuer:
                issuer_str = short_address(issuer)
        finally:
            currency_str = f"{currency_str}.{issuer_str}"

        if value is not None:
            currency_str = f"{value} {currency_str}"

    return currency_str

def format_bid_ask(offer_create_txn: OfferCreate) -> dict[str, str]:
    return {"pays": format_currency(offer_create_txn.taker_pays), "gets": format_currency(offer_create_txn.taker_gets)}
