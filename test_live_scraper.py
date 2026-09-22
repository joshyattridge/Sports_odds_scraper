"""Local command-line runner for the live_scraper library."""

import argparse
import os

from live_scraper import run


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the AI-generated live odds scraper")
    parser.add_argument("url", help="Live odds page URL")
    parser.add_argument("--market", required=True, help="Market to scrape, e.g. moneyline")
    parser.add_argument("--retries", type=int, default=5, help="Maximum AI generation attempts")
    parser.add_argument("--wait", type=float, default=10, help="Seconds to wait after initial page load")
    args = parser.parse_args()
    if not os.getenv("OPENAI_API_KEY"):
        parser.error("OPENAI_API_KEY is required")
    return run(args.url, args.market, args.retries, args.wait)


if __name__ == "__main__":
    raise SystemExit(main())
