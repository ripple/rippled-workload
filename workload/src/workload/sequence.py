"""Per-account sequence tracker for fire-and-forget transaction submission.

Prevents tefPAST_SEQ cascades when multiple transactions from the same account
are submitted before the ledger closes. Lazily fetches the starting sequence
from the ledger, then increments in-memory for subsequent calls.
"""

from __future__ import annotations

from xrpl.asyncio.account import get_next_valid_seq_number
from xrpl.asyncio.clients import AsyncJsonRpcClient

from workload import logging

log = logging.getLogger(__name__)


class SequenceTracker:
    """Per-account sequence counter. Lazily initialized from the ledger."""

    def __init__(self, client: AsyncJsonRpcClient) -> None:
        self._client = client
        self._seqs: dict[str, int] = {}

    async def next_seq(self, address: str) -> int:
        """Return the next sequence for this account and advance the counter.

        First call per account fetches from the ledger via RPC.
        Subsequent calls return the in-memory counter (no RPC).
        """
        if address not in self._seqs:
            seq = await get_next_valid_seq_number(address, self._client)
            self._seqs[address] = seq
            log.debug("SeqTracker: initialized %s at seq %d", address, seq)
        seq = self._seqs[address]
        self._seqs[address] = seq + 1
        return seq

    def reset(self, address: str) -> None:
        """Force re-initialization on next call (e.g., after a known desync)."""
        self._seqs.pop(address, None)
