"""Public result models returned by the odds scraper."""

from dataclasses import dataclass
from datetime import datetime
from typing import Tuple


@dataclass(frozen=True)
class Selection:
    name: str
    odds: float
    formatted_odds: str
    last_changed_at: datetime


@dataclass(frozen=True)
class OddsEvent:
    event: str
    selections: Tuple[Selection, ...]


@dataclass(frozen=True)
class OddsSnapshot:
    scraped_at: datetime
    events: Tuple[OddsEvent, ...]
