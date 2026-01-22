"""Scraping logic using trained AutoScraper rules."""

import logging
from pathlib import Path
from typing import List, Optional

from autoscraper import AutoScraper

from .fetch import fetch_page, get_domain
from .models import RawExtractionResult
from .train import DEFAULT_RULES_DIR, load_scraper

logger = logging.getLogger(__name__)


def scrape_odds(
    url: str,
    *,
    scraper: Optional[AutoScraper] = None,
    rules_dir: Path = DEFAULT_RULES_DIR,
    group_by_alias: bool = True,
    use_browser: bool = False,
    wait_time: float = 3.0,
) -> RawExtractionResult:
    """
    Extract raw odds data from a URL using trained rules.

    Args:
        url: The bookmaker event page URL.
        scraper: Optional pre-loaded AutoScraper instance.
        rules_dir: Directory containing saved rules.
        group_by_alias: Whether to group results by rule alias.
        use_browser: If True, use Playwright to render JavaScript.
        wait_time: Seconds to wait for JS to load (browser mode only).

    Returns:
        RawExtractionResult with extracted text candidates.

    Raises:
        ValueError: If no rules exist for the domain.
    """
    domain = get_domain(url)
    logger.info("Scraping URL: %s (domain: %s)", url, domain)

    # Load scraper if not provided
    if scraper is None:
        scraper = load_scraper(domain, rules_dir=rules_dir)

    if scraper is None:
        raise ValueError(
            f"No trained rules for domain '{domain}'. "
            f"Train first using train_scraper()."
        )

    # Fetch page HTML
    html = fetch_page(url, use_browser=use_browser, wait_time=wait_time)

    # Extract using learned rules
    if group_by_alias:
        extracted = scraper.get_result_similar(url, html=html, grouped=True)
        # Convert to dict with string keys
        extracted_data = {str(k): list(v) for k, v in extracted.items()} if extracted else {}
    else:
        extracted = scraper.get_result_similar(url, html=html)
        extracted_data = {"results": extracted if extracted else []}

    logger.info("Extracted %d rule groups", len(extracted_data))
    for key, values in extracted_data.items():
        logger.debug("  %s: %d values", key, len(values))

    return RawExtractionResult(
        url=url,
        domain=domain,
        extracted_data=extracted_data,
    )


def scrape_with_exact_match(
    url: str,
    *,
    scraper: Optional[AutoScraper] = None,
    rules_dir: Path = DEFAULT_RULES_DIR,
) -> RawExtractionResult:
    """
    Extract data using exact match (stricter than similar).

    Args:
        url: The bookmaker event page URL.
        scraper: Optional pre-loaded AutoScraper instance.
        rules_dir: Directory containing saved rules.

    Returns:
        RawExtractionResult with extracted text candidates.
    """
    domain = get_domain(url)

    if scraper is None:
        scraper = load_scraper(domain, rules_dir=rules_dir)

    if scraper is None:
        raise ValueError(f"No trained rules for domain '{domain}'.")

    html = fetch_page(url)
    extracted = scraper.get_result_exact(url, html=html, grouped=True)
    extracted_data = {str(k): list(v) for k, v in extracted.items()} if extracted else {}

    return RawExtractionResult(
        url=url,
        domain=domain,
        extracted_data=extracted_data,
    )


def deduplicate_results(result: RawExtractionResult) -> RawExtractionResult:
    """Remove duplicate values from extraction results while preserving order."""
    deduped_data = {}

    for key, values in result.extracted_data.items():
        seen = set()
        unique = []
        for v in values:
            if v not in seen:
                seen.add(v)
                unique.append(v)
        deduped_data[key] = unique

    return RawExtractionResult(
        url=result.url,
        domain=result.domain,
        extracted_data=deduped_data,
    )


def flatten_results(result: RawExtractionResult) -> List[str]:
    """Flatten all extracted values into a single list."""
    all_values = []
    for values in result.extracted_data.values():
        all_values.extend(values)
    return all_values
