from xrpl.models.amounts import IssuedCurrencyAmount
from xrpl.models.currencies import IssuedCurrency

from workload import logger


def short_address(address: str) -> str:
    length = 5
    sep = "..."
    return f"{address[:length]}{sep}{address[-length:]}"


def format_currency(currency: IssuedCurrency | IssuedCurrencyAmount | str | dict[str, str], short: bool = False) -> str:
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
            issuer_str = short_address(issuer) if short else issuer
        finally:
            currency_str = f"{currency_str}.{issuer_str}"

        if value is not None:
            currency_str = f"{value} {currency_str}"

    return currency_str


def issue_currencies(issuer: str, currency_code: list[str]) -> list[IssuedCurrency]:
    """Use a fixed set of currency codes to create IssuedCurrencies for a specific gateway.

    Args:
        issuer (str): Account_id of the gateway for all currencies
        currency_code (list[str], optional): _description_. Defaults to config.currency_codes.

    Returns:
        list[IssuedCurrency]: List of IssuedCurrencies a gateway provides

    """
    # TODO: format_currency()
    logger.info("Issuing %s %s", issuer, currency_code)
    return [IssuedCurrency(issuer=issuer, currency=cc) for cc in currency_code]
