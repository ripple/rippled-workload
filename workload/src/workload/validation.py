"""Validation data types shared by workload_core and sqlite_store."""

from dataclasses import dataclass
from enum import StrEnum, auto


class ValidationSrc(StrEnum):
    POLL = auto()
    WS = auto()


@dataclass
class ValidationRecord:
    txn: str
    seq: int
    src: str
