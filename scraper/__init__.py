"""Sports betting odds scraper using AutoScraper and Claude for analysis."""

from .models import Event, Market, Selection, ScrapedOdds, RawExtractionResult
from .fetch import fetch_page, get_domain
from .train import train_scraper, load_scraper, save_scraper
from .scrape import scrape_odds, deduplicate_results
from .analyze import analyze_page_for_training, format_extracted_odds
from .monitor import OddsMonitor, monitor_odds

__all__ = [
    "Event",
    "Market",
    "Selection",
    "ScrapedOdds",
    "RawExtractionResult",
    "fetch_page",
    "get_domain",
    "train_scraper",
    "load_scraper",
    "save_scraper",
    "scrape_odds",
    "deduplicate_results",
    "analyze_page_for_training",
    "format_extracted_odds",
    "OddsMonitor",
    "monitor_odds",
]
