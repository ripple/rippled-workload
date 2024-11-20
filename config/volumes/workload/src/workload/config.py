import tomllib

config = """

init_msg = "Workload initialization complete"

transactions = [
    "offers",
    "checks",
    "nfts",
    "escrow",
    "payments",
]

trust_limit = 1e15
token_distribution = 1e6

number_of_accounts = 4
number_of_gateways = 2

add_accounts_after_n_ledgers = 4
additional_accounts_per_round = 4

verbose = false

currency_codes = [
    "BTC",
    "ETH",
    "PLN",
    "USD",
]

[rippled]

    timeout = 600
    ip = "172.23.0.9"
    json_rpc_port = "5005"
    ws_port = "6005"

"""

class Config:
    def __init__(self) -> None:
        for k, v in tomllib.loads(config).items():
            setattr(self, k, v)


if __name__ == "__main__":
    pass


# More currency codes
"""
CURRENCY_CODES = [
    "ADA",
    "BHD",
    "BTC",
    "CNY",
    "DEM",
    "ETH",
    "GBP",
    "JOD",
    "KWD",
    "OMR",
    "SOL",
    "TRX",
    "USD",
]
"""
