"""Public result models returned by the odds scraper."""

from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class Selection:
    name: str
    odds: float


@dataclass(frozen=True)
class OddsEvent:
    event: str
    selections: Tuple[Selection, ...]
