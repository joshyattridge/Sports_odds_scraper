"""Local command-line runner for the live_scraper library."""

import argparse
import asyncio
import os

from sports_odds_scraper import OddsMonitor, OddsSnapshot


async def print_change(snapshot: OddsSnapshot) -> None:
    stamp = snapshot.scraped_at.isoformat(timespec="seconds")
    for event in snapshot.events:
        prices = " | ".join(
            f"{selection.name}: {selection.formatted_odds} [{selection.status}] "
            f"(since {selection.last_changed_at.isoformat(timespec='seconds')})"
            for selection in event.selections
        )
        print(f"{stamp} | {event.event} | {prices}", flush=True)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Run the AI-generated live odds scraper")
    parser.add_argument("url", help="Live odds page URL")
    parser.add_argument("--market", required=True, help="Market to scrape, e.g. moneyline")
    parser.add_argument("--retries", type=int, default=5, help="Maximum AI generation attempts")
    parser.add_argument("--wait", type=float, default=30, help="Seconds to wait after initial page load")
    parser.add_argument("--odds-format", choices=["decimal", "fraction"], default="decimal")
    parser.add_argument("--poll-interval", type=float, default=1.0, help="Fallback poll interval in seconds")
    args = parser.parse_args()
    if not os.getenv("OPENAI_API_KEY"):
        parser.error("OPENAI_API_KEY is required")
    scraper = OddsMonitor(
        args.url,
        market=args.market,
        api_key=os.environ["OPENAI_API_KEY"],
        model="gpt-6-luna",
        odds_format=args.odds_format,
        retries=args.retries,
        wait=args.wait,
        poll_interval=args.poll_interval,
    )
    await scraper.run(on_snapshot=print_change)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
