"""Local command-line runner for the live_scraper library."""

import argparse
import os
from datetime import datetime, timezone

from sports_odds_scraper import OddsMonitor, OddsEvent


def print_change(change: OddsEvent) -> None:
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    prices = " | ".join(f"{selection.name}: {selection.odds:g}" for selection in change.selections)
    print(f"{stamp} | {change.event} | {prices}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the AI-generated live odds scraper")
    parser.add_argument("url", help="Live odds page URL")
    parser.add_argument("--market", required=True, help="Market to scrape, e.g. moneyline")
    parser.add_argument("--retries", type=int, default=5, help="Maximum AI generation attempts")
    parser.add_argument("--wait", type=float, default=10, help="Seconds to wait after initial page load")
    args = parser.parse_args()
    if not os.getenv("OPENAI_API_KEY"):
        parser.error("OPENAI_API_KEY is required")
    scraper = OddsMonitor(
        args.url,
        market=args.market,
        api_key=os.environ["OPENAI_API_KEY"],
        retries=args.retries,
        wait=args.wait,
    )
    return scraper.run(on_change=print_change)


if __name__ == "__main__":
    raise SystemExit(main())
