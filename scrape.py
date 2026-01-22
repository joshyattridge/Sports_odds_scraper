#!/usr/bin/env python3
"""
Odds Scraper - Simple one-command interface.

Usage:
    python scrape.py --url "https://bookmaker.com/event" --market h2h_3way

First run: Claude analyzes the page and learns the pattern
Subsequent runs: Auto-extracts using learned pattern (no Claude needed)
"""

import argparse
import hashlib
import json
import logging
import os
import re
import sys
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

RULES_DIR = "rules"


def to_decimal_odds(odds: str) -> str:
    """Convert any odds format to decimal."""
    odds = odds.strip()
    
    # Already decimal (e.g., "1.75", "2.50")
    if re.match(r'^\d+\.\d+$', odds):
        return odds
    
    # Fractional (e.g., "4/6", "11/8")
    if '/' in odds:
        try:
            num, den = odds.split('/')
            decimal = (float(num) / float(den)) + 1
            return f"{decimal:.3f}"
        except (ValueError, ZeroDivisionError):
            return odds
    
    # American positive (e.g., "+150")
    if odds.startswith('+'):
        try:
            american = float(odds[1:])
            decimal = (american / 100) + 1
            return f"{decimal:.3f}"
        except ValueError:
            return odds
    
    # American negative (e.g., "-200")
    if odds.startswith('-'):
        try:
            american = abs(float(odds[1:]))
            decimal = (100 / american) + 1
            return f"{decimal:.3f}"
        except (ValueError, ZeroDivisionError):
            return odds
    
    # Plain integer (treat as decimal already, e.g., "2")
    if odds.isdigit():
        return f"{float(odds):.3f}"
    
    # Unknown format, return as-is
    return odds


def get_rule_filename(url: str, market: str) -> str:
    """Generate a unique filename for URL + market combination."""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    domain = parsed.netloc.replace("www.", "")
    # Hash the full URL path to get unique identifier per event
    url_hash = hashlib.md5(parsed.path.encode()).hexdigest()[:12]
    return f"{domain}_{market}_{url_hash}_rule.json"


def rule_exists(url: str, market: str) -> bool:
    """Check if a rule exists for this URL + market."""
    filepath = os.path.join(RULES_DIR, get_rule_filename(url, market))
    return os.path.exists(filepath)


def load_rule(url: str, market: str):
    """Load rule for URL + market."""
    from scraper.smart_extract import SmartRule
    
    filepath = os.path.join(RULES_DIR, get_rule_filename(url, market))
    
    if not os.path.exists(filepath):
        return None
    
    with open(filepath, "r") as f:
        data = json.load(f)
    
    return SmartRule(
        market_name=data["market_name"],
        odds_pattern=data["odds_pattern"],
        name_position=data["name_position"],
        name_separator=data.get("name_separator", " "),
        sample_odds=data.get("sample_odds", []),
        description=data.get("description", ""),
        group_size=data.get("group_size", 3),
        market_type=data.get("market_type", ""),
        selection_labels=data.get("selection_labels", []),
        start_marker=data.get("start_marker", ""),
        end_marker=data.get("end_marker", ""),
    )


def save_rule(url: str, market: str, rule):
    """Save rule for URL + market."""
    os.makedirs(RULES_DIR, exist_ok=True)
    
    filepath = os.path.join(RULES_DIR, get_rule_filename(url, market))
    
    data = {
        "market_name": rule.market_name,
        "odds_pattern": rule.odds_pattern,
        "name_position": rule.name_position,
        "name_separator": rule.name_separator,
        "sample_odds": rule.sample_odds,
        "description": rule.description,
        "group_size": rule.group_size,
        "market_type": rule.market_type,
        "selection_labels": rule.selection_labels,
        "start_marker": rule.start_marker,
        "end_marker": rule.end_marker,
        "source_url": url,  # Store original URL for reference
    }
    
    with open(filepath, "w") as f:
        json.dump(data, f, indent=2)
    
    return filepath


def setup(url: str, market: str, wait: float, headless: bool = True) -> int:
    """Setup: Learn patterns from the page using Claude."""
    from scraper.fetch import fetch_snapshot
    from scraper.smart_extract import (
        get_market_config,
        SmartRule,
        analyze_snapshot_for_odds,
        learn_pattern_from_snapshot,
        extract_with_smart_rule,
    )
    
    market_config = get_market_config(market)
    if not market_config:
        print(f"❌ Unknown market: {market}")
        print(f"   Use: h2h_3way, h2h_2way, over_under_X.X, btts")
        return 1
    
    print(f"🔍 First time for this URL + {market}")
    print(f"   Fetching page and learning patterns...")
    
    snapshot, _ = fetch_snapshot(url, wait_time=wait, headless=headless)
    
    if len(snapshot) < 100:
        print(f"\n❌ Page didn't load properly ({len(snapshot)} chars)")
        print(f"   Try --wait 10 or check if site is geo-blocked")
        return 1
    
    print(f"🤖 Asking Claude to identify {market_config['name']} odds...")
    
    try:
        market_name, odds_pattern, odds_found, start_marker, end_marker = analyze_snapshot_for_odds(
            snapshot, market_config['prompt']
        )
    except Exception as e:
        print(f"\n❌ Failed to analyze page: {e}")
        return 1
    
    expected_count = market_config['selections']
    if len(odds_found) > expected_count:
        odds_found = odds_found[:expected_count]
    
    if len(odds_found) < expected_count:
        print(f"\n⚠️  Expected {expected_count} odds but found {len(odds_found)}")
        if not odds_found:
            print(f"   Try --wait 10 or check if site requires login")
            return 1
    
    print(f"   Found: {', '.join(odds_found)}")
    if start_marker:
        print(f"   Context: \"{start_marker}\" → \"{end_marker}\"")
    
    # Learn pattern
    name_position, name_separator, _ = learn_pattern_from_snapshot(
        snapshot, odds_found, odds_pattern
    )
    
    # Create rule
    rule = SmartRule(
        market_name=market_config['name'],
        odds_pattern=odds_pattern,
        name_position=name_position,
        name_separator=name_separator,
        sample_odds=odds_found,
        description=f"Market: {market}",
        group_size=expected_count,
        market_type=market,
        selection_labels=market_config['labels'],
        start_marker=start_marker,
        end_marker=end_marker,
    )
    
    # Test extraction
    result = extract_with_smart_rule(snapshot, rule)
    
    if not result.selections:
        print(f"\n⚠️  Extraction test failed")
        return 1
    
    # Save rule
    filepath = save_rule(url, market, rule)
    print(f"✅ Learned pattern saved to {filepath}")
    
    # Return the result
    return scrape_with_rule(result)


def scrape_with_rule(market) -> int:
    """Print scrape results with decimal odds."""
    print(f"\n{market.market_name}:")
    print("-" * 40)
    for sel in market.selections:
        decimal_odds = to_decimal_odds(sel.odds)
        print(f"  {sel.name}: {decimal_odds}")
    return 0


def scrape(url: str, market: str, wait: float, headless: bool = True) -> int:
    """Scrape using existing rule."""
    from scraper.fetch import fetch_snapshot
    from scraper.smart_extract import extract_with_smart_rule
    
    rule = load_rule(url, market)
    if not rule:
        # This shouldn't happen as we check before calling
        return setup(url, market, wait, headless)
    
    snapshot, _ = fetch_snapshot(url, wait_time=wait, headless=headless)
    
    if len(snapshot) < 100:
        print(f"❌ Page didn't load properly")
        return 1
    
    result = extract_with_smart_rule(snapshot, rule)
    
    if not result.selections:
        print(f"⚠️  No odds found - pattern may need re-learning")
        print(f"   Delete rules/{get_rule_filename(url, market)} and try again")
        return 1
    
    return scrape_with_rule(result)


def watch_scrape(url: str, market: str, wait: float, interval: int, log_file: str, headless: bool = True) -> int:
    """Continuously scrape and log odds using persistent browser."""
    import time
    import csv
    from scraper.fetch import PersistentBrowser
    from scraper.smart_extract import extract_with_smart_rule
    
    # Ensure rule exists first
    if not rule_exists(url, market):
        print("Setting up rule first...")
        result = setup(url, market, wait, headless)
        if result != 0:
            return result
    
    rule = load_rule(url, market)
    
    # Setup CSV log file
    file_exists = os.path.exists(log_file)
    
    print(f"\n📊 Watching {market} odds")
    print(f"   Interval: {interval}s | Refresh wait: {wait}s")
    print(f"   Logging to: {log_file}")
    print(f"   Press Ctrl+C to stop\n")
    
    try:
        with PersistentBrowser(url, wait_time=wait, headless=headless) as browser:
            while True:
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                
                try:
                    snapshot = browser.refresh_snapshot()
                    result = extract_with_smart_rule(snapshot, rule)
                    
                    if result.selections:
                        # Build row data with decimal odds
                        row = {"timestamp": timestamp}
                        odds_str = []
                        for sel in result.selections:
                            decimal_odds = to_decimal_odds(sel.odds)
                            row[sel.name] = decimal_odds
                            odds_str.append(f"{sel.name}: {decimal_odds}")
                        
                        # Write to CSV
                        with open(log_file, "a", newline="") as f:
                            writer = csv.DictWriter(f, fieldnames=row.keys())
                            if not file_exists:
                                writer.writeheader()
                                file_exists = True
                            writer.writerow(row)
                        
                        # Print to console
                        print(f"[{timestamp}] {' | '.join(odds_str)}")
                    else:
                        print(f"[{timestamp}] ⚠️  No odds found")
                        
                except Exception as e:
                    print(f"[{timestamp}] ❌ Error: {e}")
                
                time.sleep(interval)
            
    except KeyboardInterrupt:
        print(f"\n\n✅ Stopped. Data saved to {log_file}")
        return 0


def validate_market(value: str) -> str:
    """Validate market type, supporting dynamic over_under lines."""
    valid_markets = ["h2h_3way", "h2h_2way", "btts"]
    
    # Check standard markets
    if value in valid_markets:
        return value
    
    # Check over_under with line (e.g., over_under_2.5)
    if value.startswith("over_under_"):
        line = value.replace("over_under_", "")
        try:
            float(line)  # Validate it's a number
            return value
        except ValueError:
            raise argparse.ArgumentTypeError(f"Invalid line: {line}. Must be a number (e.g., over_under_2.5)")
    
    raise argparse.ArgumentTypeError(
        f"Invalid market: {value}. Use h2h_3way, h2h_2way, btts, or over_under_X.X (e.g., over_under_2.5)"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scrape betting odds from any bookmaker",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Markets:
  h2h_3way        Match Result (Home/Draw/Away)
  h2h_2way        Moneyline (Home/Away)
  over_under_X.X  Over/Under Goals (e.g., over_under_2.5, over_under_1.5)
  btts            Both Teams To Score

Examples:
  python scrape.py --url "https://bookmaker.com/event" --market h2h_3way
  python scrape.py -u "https://bookmaker.com/event" -m over_under_2.5
  python scrape.py -u "https://bookmaker.com/event" -m h2h_3way --watch 30
  python scrape.py -u "https://bookmaker.com/event" -m h2h_3way --visible  # For tricky sites
"""
    )
    parser.add_argument(
        "--url", "-u",
        required=True,
        help="Event page URL"
    )
    parser.add_argument(
        "--market", "-m",
        required=True,
        type=validate_market,
        help="Market type (h2h_3way, h2h_2way, over_under_X.X, btts)"
    )
    parser.add_argument(
        "--wait", "-w",
        type=float,
        default=5.0,
        help="Seconds to wait for page load (default: 5)"
    )
    parser.add_argument(
        "--watch",
        type=int,
        metavar="SECONDS",
        help="Watch mode: scrape every N seconds continuously"
    )
    parser.add_argument(
        "--log",
        type=str,
        default="odds_log.csv",
        help="CSV file to log odds (default: odds_log.csv)"
    )
    parser.add_argument(
        "--visible",
        action="store_true",
        help="Show browser window (helps bypass bot detection on some sites)"
    )
    
    args = parser.parse_args()
    headless = not args.visible
    
    # Watch mode
    if args.watch:
        return watch_scrape(args.url, args.market, args.wait, args.watch, args.log, headless)
    
    # Single scrape
    if rule_exists(args.url, args.market):
        return scrape(args.url, args.market, args.wait, headless)
    else:
        return setup(args.url, args.market, args.wait, headless)


if __name__ == "__main__":
    sys.exit(main())
