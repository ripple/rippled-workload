"""Per-account sequence tracker; prevents tefPAST_SEQ cascades in fire-and-forget submission."""

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
        """Return the next sequence and advance; first call per account hits RPC."""
        if address not in self._seqs:
            seq = await get_next_valid_seq_number(address, self._client)
            self._seqs[address] = seq
            log.debug("SeqTracker: initialized %s at seq %d", address, seq)
        seq = self._seqs[address]
        self._seqs[address] = seq + 1
        return seq

    def advance(self, address: str, by: int) -> None:
        """Bump tracked sequence by ``by`` (no-op if untracked). For TicketCreate:
        it advances Sequence by TicketCount + 1 but next_seq only added the +1,
        so callers pass ``by = TicketCount`` to keep a reused account aligned."""
        if address in self._seqs:
            self._seqs[address] += by

    def reset(self, address: str) -> None:
        """Force re-initialization on next call (e.g., after a known desync)."""
        self._seqs.pop(address, None)
