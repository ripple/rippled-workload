"""Balance tracking for workload accounts."""


class BalanceTracker:
    """Tracks in-memory balances for workload accounts.

    Keys per account:
    - "XRP"                  → drops (float)
    - (currency, issuer)     → IOU amount (float)
    - ("MPT", mpt_id)        → MPToken amount (float)
    """

    def __init__(self) -> None:
        self._balances: dict[str, dict[str | tuple[str, str], float]] = {}

    @property
    def data(self) -> dict[str, dict[str | tuple[str, str], float]]:
        """Direct access to the underlying dict (for passing to TxnContext)."""
        return self._balances

    def get(self, account: str, currency: str, issuer: str | None = None) -> float:
        if account not in self._balances:
            return 0.0
        if currency == "XRP":
            return self._balances[account].get("XRP", 0.0)
        key: str | tuple[str, str] = (currency, issuer) if issuer else currency
        return self._balances[account].get(key, 0.0)

    def set(self, account: str, currency: str, value: float, issuer: str | None = None) -> None:
        if account not in self._balances:
            self._balances[account] = {}
        if currency == "XRP":
            self._balances[account]["XRP"] = value
        else:
            key: str | tuple[str, str] = (currency, issuer) if issuer else currency
            self._balances[account][key] = value

    def update(self, account: str, currency: str, delta: float, issuer: str | None = None) -> None:
        self.set(account, currency, self.get(account, currency, issuer) + delta, issuer)
