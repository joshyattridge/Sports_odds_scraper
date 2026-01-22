"""AutoScraper training and rule management."""

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

from autoscraper import AutoScraper

from .fetch import fetch_page, get_domain

logger = logging.getLogger(__name__)

DEFAULT_RULES_DIR = Path("rules")


def get_rules_path(domain: str, rules_dir: Path = DEFAULT_RULES_DIR) -> Path:
    """Get the file path for a domain's rules."""
    rules_dir.mkdir(parents=True, exist_ok=True)
    # Sanitise domain for filename
    safe_domain = domain.replace(".", "_").replace(":", "_")
    return rules_dir / f"{safe_domain}.json"


def train_scraper(
    url: str,
    wanted_list: List[str],
    *,
    rules_dir: Path = DEFAULT_RULES_DIR,
    save_rules: bool = True,
    use_browser: bool = False,
    wait_time: float = 3.0,
) -> AutoScraper:
    """
    Train AutoScraper on a URL with example values.

    Args:
        url: The bookmaker event page URL.
        wanted_list: Example strings to find (team names, odds values, etc.).
        rules_dir: Directory to save learned rules.
        save_rules: Whether to persist rules to disk.
        use_browser: If True, use Playwright to render JavaScript.
        wait_time: Seconds to wait for JS to load (browser mode only).

    Returns:
        Trained AutoScraper instance.
    """
    logger.info("Training scraper for URL: %s", url)
    logger.info("Wanted list: %s", wanted_list)

    # Fetch the page HTML
    html = fetch_page(url, use_browser=use_browser, wait_time=wait_time)

    # Create and train scraper
    scraper = AutoScraper()
    result = scraper.build(url=url, wanted_list=wanted_list, html=html)

    if not result:
        logger.warning("AutoScraper found no matches for the wanted list")
    else:
        logger.info("AutoScraper learned %d rule groups", len(scraper.get_result_exact(url, html=html, grouped=True)))

    # Optionally save rules
    if save_rules:
        domain = get_domain(url)
        rules_path = get_rules_path(domain, rules_dir)
        save_scraper(scraper, rules_path)
        logger.info("Saved rules to: %s", rules_path)

    return scraper


def save_scraper(scraper: AutoScraper, path: Path) -> None:
    """
    Save AutoScraper rules to a JSON file.

    Args:
        scraper: Trained AutoScraper instance.
        path: File path to save rules.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    scraper.save(str(path))
    logger.debug("Saved scraper rules to %s", path)


def load_scraper(
    domain: str,
    *,
    rules_dir: Path = DEFAULT_RULES_DIR,
) -> Optional[AutoScraper]:
    """
    Load AutoScraper rules for a domain.

    Args:
        domain: The domain to load rules for.
        rules_dir: Directory containing saved rules.

    Returns:
        Loaded AutoScraper instance, or None if no rules exist.
    """
    rules_path = get_rules_path(domain, rules_dir)

    if not rules_path.exists():
        logger.warning("No rules found for domain: %s", domain)
        return None

    scraper = AutoScraper()
    scraper.load(str(rules_path))

    logger.info("Loaded rules from: %s", rules_path)
    return scraper


def list_trained_domains(rules_dir: Path = DEFAULT_RULES_DIR) -> List[str]:
    """List all domains with saved rules."""
    if not rules_dir.exists():
        return []

    domains = []
    for path in rules_dir.glob("*.json"):
        # Convert filename back to domain
        domain = path.stem.replace("_", ".")
        domains.append(domain)

    return sorted(domains)


def get_rule_info(domain: str, rules_dir: Path = DEFAULT_RULES_DIR) -> Optional[Dict]:
    """Get metadata about saved rules for a domain."""
    rules_path = get_rules_path(domain, rules_dir)

    if not rules_path.exists():
        return None

    with open(rules_path) as f:
        data = json.load(f)

    return {
        "domain": domain,
        "path": str(rules_path),
        "stack_count": len(data.get("stack_list", [])),
        "file_size_bytes": rules_path.stat().st_size,
    }
