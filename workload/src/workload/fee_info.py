"""Fee escalation data structures.

NOTE: If we accumulate a few more domain data structures, move this into models.py
"""

from dataclasses import dataclass


@dataclass
class FeeInfo:
    """Current fee escalation state from rippled fee command.

    All fee values are in drops. Sizes and counts reflect current open ledger state.
    Note: current_ledger_size and current_queue_size change rapidly (per transaction).

    See reference/FeeEscalation.md for detailed documentation on fee escalation.
    """

    expected_ledger_size: int
    current_ledger_size: int
    current_queue_size: int
    max_queue_size: int
    base_fee: int  # drops
    median_fee: int  # drops
    minimum_fee: int  # drops
    open_ledger_fee: int  # drops
    ledger_current_index: int

    @classmethod
    def from_fee_result(cls, result: dict) -> "FeeInfo":
        """Parse fee command result into FeeInfo.

        Args:
            result: The 'result' field from xrpl.models.requests.Fee response

        Returns:
            FeeInfo instance with parsed values
        """
        drops = result["drops"]
        return cls(
            expected_ledger_size=int(result["expected_ledger_size"]),
            current_ledger_size=int(result["current_ledger_size"]),
            current_queue_size=int(result["current_queue_size"]),
            max_queue_size=int(result["max_queue_size"]),
            base_fee=int(drops["base_fee"]),
            median_fee=int(drops["median_fee"]),
            minimum_fee=int(drops["minimum_fee"]),
            open_ledger_fee=int(drops["open_ledger_fee"]),
            ledger_current_index=int(result["ledger_current_index"]),
        )
