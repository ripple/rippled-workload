"""AMM pool registry and DEX metrics tracking."""

import logging
from dataclasses import dataclass, field

log = logging.getLogger("workload")


@dataclass
class DEXMetrics:
    pools_created: int = 0
    total_deposits: int = 0
    total_withdrawals: int = 0
    total_offers: int = 0
    pool_snapshots: list[dict] = field(default_factory=list)
    last_poll_ledger: int = 0
    total_xrp_locked_drops: int = 0
    active_pools: int = 0


class AMMPoolRegistry:
    """Tracks registered AMM pools and their LP holders."""

    def __init__(self) -> None:
        self._pools: list[dict] = []
        self._pool_ids: set[frozenset[str]] = set()

    @staticmethod
    def _asset_id(asset: dict) -> str:
        if asset.get("currency") == "XRP":
            return "XRP"
        return f"{asset['currency']}.{asset.get('issuer', '')}"

    @property
    def pools(self) -> list[dict]:
        return self._pools

    @property
    def pool_ids(self) -> set[frozenset[str]]:
        return self._pool_ids

    def __len__(self) -> int:
        return len(self._pools)

    def __bool__(self) -> bool:
        return bool(self._pools)

    def register(self, asset1: dict, asset2: dict, creator: str) -> None:
        """Register a new AMM pool. Caller is responsible for dedup via pool_ids."""
        pool_info = {"asset1": asset1, "asset2": asset2, "creator": creator, "lp_holders": [creator]}
        self._pools.append(pool_info)
        id1 = self._asset_id(asset1)
        id2 = self._asset_id(asset2)
        self._pool_ids.add(frozenset([id1, id2]))

    def add_lp_holder(self, asset1: dict, asset2: dict, account: str) -> None:
        """Track account as an LP holder for the matching pool."""
        id1 = self._asset_id(asset1)
        id2 = self._asset_id(asset2)
        target = frozenset([id1, id2])
        for pool in self._pools:
            pid1 = self._asset_id(pool["asset1"])
            pid2 = self._asset_id(pool["asset2"])
            if frozenset([pid1, pid2]) == target and account not in pool["lp_holders"]:
                pool["lp_holders"].append(account)
                break
