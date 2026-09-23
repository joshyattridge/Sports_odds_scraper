"""Exceptions raised by sports_odds_scraper."""


class SportsOddsError(Exception):
    """Base exception for the package."""


class ScraperGenerationError(SportsOddsError):
    """The model could not generate a validated extractor."""


class ScraperValidationError(SportsOddsError):
    """Generated extraction output failed validation."""


class BrowserError(SportsOddsError):
    """The browser could not load or monitor a page."""
