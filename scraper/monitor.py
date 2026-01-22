"""Continuous odds monitoring with periodic scraping."""

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .fetch import fetch_page, get_domain
from .scrape import scrape_odds, deduplicate_results, flatten_results
from .train import load_scraper

logger = logging.getLogger(__name__)


class OddsMonitor:
    """Monitor and record odds changes over time."""

    def __init__(
        self,
        url: str,
        rules_dir: Path = Path("rules"),
        output_file: Optional[Path] = None,
        use_browser: bool = False,
        wait_time: float = 2.0,
    ):
        self.url = url
        self.domain = get_domain(url)
        self.rules_dir = rules_dir
        self.output_file = output_file or Path(f"odds_{self.domain}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl")
        self.scraper = None
        self.records: List[Dict] = []
        self._running = False
        self.use_browser = use_browser
        self.wait_time = wait_time

    def load_rules(self) -> bool:
        """Load scraper rules for this domain."""
        self.scraper = load_scraper(self.domain, rules_dir=self.rules_dir)
        return self.scraper is not None

    def scrape_once(self) -> Dict[str, List[str]]:
        """Perform a single scrape and return raw data."""
        if self.scraper is None:
            raise ValueError(f"No rules loaded for {self.domain}")

        result = scrape_odds(
            self.url,
            scraper=self.scraper,
            rules_dir=self.rules_dir,
            use_browser=self.use_browser,
            wait_time=self.wait_time,
        )
        result = deduplicate_results(result)
        return result.extracted_data

    def record_odds(self, data: Dict[str, List[str]]) -> Dict:
        """Record odds with timestamp."""
        record = {
            "timestamp": datetime.now().isoformat(),
            "url": self.url,
            "data": data,
        }
        self.records.append(record)

        # Append to file
        with open(self.output_file, "a") as f:
            f.write(json.dumps(record) + "\n")

        return record

    def run(
        self,
        interval_seconds: float = 1.0,
        max_iterations: Optional[int] = None,
        on_update: Optional[Callable[[Dict], None]] = None,
    ) -> None:
        """
        Continuously monitor odds.

        Args:
            interval_seconds: Time between scrapes.
            max_iterations: Stop after N iterations (None = run forever).
            on_update: Callback function called with each new record.
        """
        if self.scraper is None:
            if not self.load_rules():
                raise ValueError(f"No rules for domain: {self.domain}")

        self._running = True
        iteration = 0

        logger.info("Starting odds monitor for %s", self.url)
        logger.info("Recording to: %s", self.output_file)
        logger.info("Interval: %.1f seconds", interval_seconds)

        try:
            while self._running:
                if max_iterations and iteration >= max_iterations:
                    break

                try:
                    data = self.scrape_once()
                    record = self.record_odds(data)

                    if on_update:
                        on_update(record)
                    else:
                        # Default: print summary
                        values = flatten_results_dict(data)
                        logger.info(
                            "[%s] Scraped %d values",
                            record["timestamp"],
                            len(values),
                        )

                except Exception as e:
                    logger.error("Scrape error: %s", e)

                iteration += 1
                time.sleep(interval_seconds)

        except KeyboardInterrupt:
            logger.info("Monitoring stopped by user")

        finally:
            self._running = False
            logger.info("Recorded %d snapshots to %s", len(self.records), self.output_file)

    def stop(self) -> None:
        """Stop the monitoring loop."""
        self._running = False


def flatten_results_dict(data: Dict[str, List[str]]) -> List[str]:
    """Flatten dict of lists to single list."""
    all_values = []
    for values in data.values():
        all_values.extend(values)
    return all_values


def monitor_odds(
    url: str,
    interval: float = 1.0,
    duration: Optional[int] = None,
    output_file: Optional[str] = None,
    rules_dir: str = "rules",
) -> Path:
    """
    Convenience function to monitor odds.

    Args:
        url: Event URL to monitor.
        interval: Seconds between scrapes.
        duration: Total seconds to run (None = forever).
        output_file: Output JSONL file path.
        rules_dir: Directory with scraper rules.

    Returns:
        Path to output file.
    """
    max_iterations = int(duration / interval) if duration else None

    monitor = OddsMonitor(
        url=url,
        rules_dir=Path(rules_dir),
        output_file=Path(output_file) if output_file else None,
    )

    monitor.run(
        interval_seconds=interval,
        max_iterations=max_iterations,
    )

    return monitor.output_file
