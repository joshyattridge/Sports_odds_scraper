"""Pydantic models for structured odds data."""

from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class Selection(BaseModel):
    """A single betting selection with name and decimal price."""

    name: str = Field(..., description="Selection name (e.g., team name, 'Draw')")
    price_decimal: float = Field(..., gt=1.0, description="Decimal odds (must be > 1.0)")


class Market(BaseModel):
    """A betting market containing multiple selections."""

    market_name: str = Field(..., description="Market type (e.g., 'Match Result', '1X2')")
    selections: List[Selection] = Field(..., min_length=1, description="List of selections")


class Event(BaseModel):
    """Sports event metadata."""

    name: str = Field(..., description="Event name (e.g., 'Team A vs Team B')")
    start_time: Optional[str] = Field(None, description="Event start time if available")


class ScrapedOdds(BaseModel):
    """Complete structured output for scraped odds."""

    event: Event
    markets: List[Market] = Field(..., min_length=1, description="List of betting markets")


class TrainingExample(BaseModel):
    """Training example for AutoScraper."""

    url: str = Field(..., description="URL of the page to train on")
    wanted_list: List[str] = Field(
        ..., min_length=1, description="Example strings to find (team names, odds values)"
    )


class RawExtractionResult(BaseModel):
    """Raw extraction result before normalisation."""

    url: str
    domain: str
    extracted_data: Dict[str, List[str]] = Field(
        default_factory=dict, description="Rule ID -> extracted values"
    )
