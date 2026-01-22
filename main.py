#!/usr/bin/env python3
"""
Odds Scraper CLI - Extract and monitor sports betting odds.

Workflow:
1. Setup: Give URL + prompt, Claude analyzes page and creates CSS selectors
2. Monitor: Continuously scrape odds using selectors (no Claude needed)

Usage:
    # One-time setup: analyze page and create selectors
    python main.py setup --url "https://bookmaker.com/event/123" \
        --prompt "get me the match winner odds"

    # Monitor odds continuously
    python main.py monitor --url "https://bookmaker.com/event/123" --browser

    # Single scrape
    python main.py scrape --url "https://bookmaker.com/event/123" --browser
"""

import argparse
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def cmd_setup(args: argparse.Namespace) -> int:
    """Analyze page with Claude using lightweight snapshot (saves tokens!)."""
    from scraper.fetch import fetch_snapshot, get_domain
    from scraper.extract import (
        analyze_page_with_snapshot, 
        extract_with_rule,
        save_rule,
    )

    print(f"🔍 Fetching page snapshot: {args.url}")
    print("   (using Playwright accessibility snapshot - saves tokens!)")
    
    snapshot, html = fetch_snapshot(args.url, wait_time=args.wait)
    print(f"   Snapshot: {len(snapshot):,} chars (vs {len(html):,} bytes HTML)")

    print(f"\n🤖 Analyzing with Claude...")
    print(f"   Prompt: \"{args.prompt}\"")

    try:
        rule = analyze_page_with_snapshot(snapshot, html, args.prompt)
    except Exception as e:
        print(f"\n❌ Failed to analyze page: {e}")
        return 1

    print(f"\n✓ Claude identified selectors:")
    print(f"   Market: {rule.market_name}")
    print(f"   Selection container: {rule.selection_selector}")
    print(f"   Name selector: {rule.name_selector}")
    print(f"   Odds selector: {rule.odds_selector}")
    if rule.description:
        print(f"   Note: {rule.description}")

    # Check for invalid selectors
    invalid_indicators = ['n/a', 'unable', 'none', 'not found', 'cannot']
    selector_invalid = any(
        indicator in (rule.selection_selector or '').lower() 
        for indicator in invalid_indicators
    ) or not rule.selection_selector or rule.selection_selector.strip() == ''
    
    if selector_invalid:
        print(f"\n❌ Claude couldn't identify valid selectors for this page.")
        print(f"   This might be because:")
        print(f"   • The page didn't load betting content (404, geo-blocked, etc.)")
        print(f"   • The page uses dynamic/obfuscated CSS classes")
        print(f"   • The betting markets haven't loaded yet (try --wait)")
        return 1

    # Test extraction
    print(f"\n🧪 Testing extraction...")
    market = extract_with_rule(html, rule)
    
    if not market.selections:
        print(f"   ⚠️  No selections found. Selectors might need adjustment.")
        print(f"   Try a different prompt or check the page structure.")
    else:
        print(f"   ✓ Found {len(market.selections)} selections:")
        for sel in market.selections[:10]:
            print(f"      • {sel.name}: {sel.odds}")
        if len(market.selections) > 10:
            print(f"      ... and {len(market.selections) - 10} more")

    # Confirm with user
    if not args.yes and market.selections:
        confirm = input("\n▶ Save these selectors? [Y/n]: ").strip().lower()
        if confirm and confirm != "y":
            print("Aborted.")
            return 1

    # Save rule
    domain = get_domain(args.url)
    rules_path = save_rule(domain, rule, rules_dir=args.rules_dir)

    print(f"\n✅ Setup complete!")
    print(f"   Rules saved to: {rules_path}")
    print(f"\n   Next steps:")
    print(f"   • Single scrape:  python main.py scrape --url \"{args.url}\" --browser")
    print(f"   • Monitor odds:   python main.py monitor --url \"{args.url}\" --browser")

    return 0


def cmd_scrape(args: argparse.Namespace) -> int:
    """Single scrape using saved selectors."""
    from scraper.fetch import fetch_page, get_domain
    from scraper.extract import load_rule, extract_with_rule

    use_browser = args.browser
    domain = get_domain(args.url)
    
    rule = load_rule(domain, rules_dir=args.rules_dir)
    if not rule:
        print(f"⚠️  No rules found for {domain}. Run setup first:")
        print(f"   python main.py setup --url \"{args.url}\" --prompt \"your request\"")
        return 1

    print(f"🔍 Scraping: {args.url}")
    if use_browser:
        print("   (using browser)")
    
    html = fetch_page(args.url, use_browser=use_browser, wait_time=args.wait)
    market = extract_with_rule(html, rule)

    print(f"\n✓ {market.market_name}:")
    print("-" * 40)

    if args.json:
        data = {
            "market": market.market_name,
            "timestamp": datetime.now().isoformat(),
            "selections": [
                {"name": s.name, "odds": s.odds} 
                for s in market.selections
            ]
        }
        print(json.dumps(data, indent=2))
    else:
        for sel in market.selections:
            print(f"  {sel.name}: {sel.odds}")

    return 0


def cmd_monitor(args: argparse.Namespace) -> int:
    """Continuously monitor odds."""
    from scraper.fetch import fetch_page, get_domain
    from scraper.extract import load_rule, extract_with_rule

    use_browser = args.browser
    domain = get_domain(args.url)
    
    rule = load_rule(domain, rules_dir=args.rules_dir)
    if not rule:
        print(f"⚠️  No rules found for {domain}. Run setup first:")
        print(f"   python main.py setup --url \"{args.url}\" --prompt \"your request\"")
        return 1

    # Setup output file
    if args.output:
        output_path = Path(args.output)
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = Path(f"odds_{domain}_{timestamp}.jsonl")
    
    print(f"📊 Starting odds monitor")
    print(f"   URL: {args.url}")
    print(f"   Market: {rule.market_name}")
    print(f"   Interval: {args.interval}s")
    if use_browser:
        print(f"   Mode: Browser (JS rendering)")
    print(f"   Output: {output_path}")
    print(f"   Press Ctrl+C to stop\n")

    iteration = 0
    records = []
    
    try:
        while args.count is None or iteration < args.count:
            iteration += 1
            
            try:
                html = fetch_page(args.url, use_browser=use_browser, wait_time=args.wait)
                market = extract_with_rule(html, rule)
                
                ts = datetime.now().isoformat()
                record = {
                    "timestamp": ts,
                    "iteration": iteration,
                    "market": market.market_name,
                    "selections": [
                        {"name": s.name, "odds": s.odds}
                        for s in market.selections
                    ]
                }
                records.append(record)
                
                # Write to file
                with open(output_path, "a") as f:
                    f.write(json.dumps(record) + "\n")
                
                # Print summary
                odds_str = " | ".join(f"{s.name}: {s.odds}" for s in market.selections[:3])
                print(f"[{ts[11:19]}] #{iteration} - {odds_str}")
                
            except Exception as e:
                logger.error(f"Error on iteration {iteration}: {e}")
                print(f"[{datetime.now().strftime('%H:%M:%S')}] ❌ Error: {e}")
            
            if args.count is None or iteration < args.count:
                time.sleep(args.interval)
                
    except KeyboardInterrupt:
        pass

    print(f"\n✓ Saved {len(records)} records to: {output_path}")
    return 0


def cmd_setup_text(args: argparse.Namespace) -> int:
    """Setup using text-based patterns - works on ANY site including obfuscated ones."""
    from scraper.fetch import fetch_snapshot, get_domain
    from scraper.text_extract import (
        analyze_snapshot_for_text,
        extract_with_text_rule,
        save_text_rule,
    )

    print(f"🔍 Fetching page snapshot: {args.url}")
    print("   (TEXT-BASED mode - works on obfuscated sites!)")
    
    snapshot, html = fetch_snapshot(args.url, wait_time=args.wait)
    print(f"   Snapshot: {len(snapshot):,} chars")

    print(f"\n🤖 Analyzing with Claude...")
    print(f"   Prompt: \"{args.prompt}\"")

    try:
        rule = analyze_snapshot_for_text(snapshot, args.prompt)
    except Exception as e:
        print(f"\n❌ Failed to analyze page: {e}")
        return 1

    print(f"\n✓ Claude identified selections:")
    print(f"   Market: {rule.market_name}")
    print(f"   Odds format: {rule.odds_pattern}")
    print(f"   Found {len(rule.selections)} selections:")
    
    for sel in rule.selections[:15]:
        print(f"      • {sel.get('name')}: {sel.get('odds')}")
    if len(rule.selections) > 15:
        print(f"      ... and {len(rule.selections) - 15} more")
    
    if rule.description:
        print(f"   Note: {rule.description}")

    if not rule.selections:
        print(f"\n❌ No selections found. The page might:")
        print(f"   • Not have loaded betting content yet (try --wait 10)")
        print(f"   • Be geo-blocked or require login")
        print(f"   • Not match your prompt")
        return 1

    # Test extraction on current snapshot
    print(f"\n🧪 Testing text extraction...")
    market = extract_with_text_rule(snapshot, rule)
    
    if market.selections:
        print(f"   ✓ Extracted {len(market.selections)} odds:")
        for sel in market.selections[:10]:
            print(f"      • {sel.name}: {sel.odds}")
    else:
        print(f"   ⚠️  Extraction test returned empty - patterns may need adjustment")

    # Confirm with user
    if not args.yes and rule.selections:
        confirm = input("\n▶ Save these text patterns? [Y/n]: ").strip().lower()
        if confirm and confirm != "y":
            print("Aborted.")
            return 1

    # Save rule
    domain = get_domain(args.url)
    rules_path = save_text_rule(domain, rule, rules_dir=args.rules_dir)

    print(f"\n✅ Setup complete!")
    print(f"   Rules saved to: {rules_path}")
    print(f"\n   Next steps:")
    print(f"   • Single scrape:  python main.py scrape-text --url \"{args.url}\"")
    print(f"   • Monitor odds:   python main.py monitor-text --url \"{args.url}\"")

    return 0


def cmd_scrape_text(args: argparse.Namespace) -> int:
    """Single scrape using text-based patterns."""
    from scraper.fetch import fetch_snapshot, get_domain
    from scraper.text_extract import load_text_rule, extract_with_text_rule

    domain = get_domain(args.url)
    
    rule = load_text_rule(domain, rules_dir=args.rules_dir)
    if not rule:
        print(f"⚠️  No text rules found for {domain}. Run setup-text first:")
        print(f"   python main.py setup-text --url \"{args.url}\" --prompt \"your request\"")
        return 1

    print(f"🔍 Scraping: {args.url}")
    
    snapshot, _ = fetch_snapshot(args.url, wait_time=args.wait)
    market = extract_with_text_rule(snapshot, rule)

    print(f"\n✓ {market.market_name}:")
    print("-" * 40)

    if args.json:
        data = {
            "market": market.market_name,
            "timestamp": datetime.now().isoformat(),
            "selections": [
                {"name": s.name, "odds": s.odds} 
                for s in market.selections
            ]
        }
        print(json.dumps(data, indent=2))
    else:
        for sel in market.selections:
            print(f"  {sel.name}: {sel.odds}")

    return 0


def cmd_monitor_text(args: argparse.Namespace) -> int:
    """Continuously monitor odds using text-based patterns."""
    from scraper.fetch import fetch_snapshot, get_domain
    from scraper.text_extract import load_text_rule, extract_with_text_rule

    domain = get_domain(args.url)
    
    rule = load_text_rule(domain, rules_dir=args.rules_dir)
    if not rule:
        print(f"⚠️  No text rules found for {domain}. Run setup-text first:")
        print(f"   python main.py setup-text --url \"{args.url}\" --prompt \"your request\"")
        return 1

    # Setup output file
    if args.output:
        output_path = Path(args.output)
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = Path(f"odds_{domain}_{timestamp}.jsonl")
    
    print(f"📊 Starting odds monitor (TEXT MODE)")
    print(f"   URL: {args.url}")
    print(f"   Market: {rule.market_name}")
    print(f"   Interval: {args.interval}s")
    print(f"   Output: {output_path}")
    print(f"   Press Ctrl+C to stop\n")

    iteration = 0
    
    try:
        while args.count is None or iteration < args.count:
            iteration += 1
            
            try:
                snapshot, _ = fetch_snapshot(args.url, wait_time=args.wait)
                market = extract_with_text_rule(snapshot, rule)
                
                ts = datetime.now().isoformat()
                record = {
                    "timestamp": ts,
                    "market": market.market_name,
                    "selections": [
                        {"name": s.name, "odds": s.odds}
                        for s in market.selections
                    ]
                }
                
                # Write to file
                with open(output_path, "a") as f:
                    f.write(json.dumps(record) + "\n")
                
                # Display summary
                odds_summary = ", ".join(
                    f"{s.name}: {s.odds}" for s in market.selections[:5]
                )
                if len(market.selections) > 5:
                    odds_summary += f" (+{len(market.selections) - 5} more)"
                
                print(f"[{iteration}] {ts[:19]} | {odds_summary}")
                
            except Exception as e:
                logger.error("Scrape failed: %s", e)
                print(f"[{iteration}] ERROR: {e}")

            if args.count is None or iteration < args.count:
                time.sleep(args.interval)

    except KeyboardInterrupt:
        print(f"\n\n✓ Monitoring stopped. {iteration} records saved to {output_path}")

    return 0


def cmd_setup_smart(args: argparse.Namespace) -> int:
    """Setup by learning patterns from the page itself."""
    from scraper.fetch import fetch_snapshot, get_domain
    from scraper.smart_extract import (
        analyze_snapshot_for_odds,
        learn_pattern_from_snapshot,
        extract_with_smart_rule,
        save_smart_rule,
        SmartRule,
    )

    # Get market config
    from scraper.smart_extract import MARKET_TYPES
    market_config = MARKET_TYPES[args.market]
    
    print(f"🔍 Fetching page snapshot: {args.url}")
    print(f"   Market: {market_config['name']} ({args.market})")
    
    snapshot, html = fetch_snapshot(args.url, wait_time=args.wait)
    print(f"   Snapshot: {len(snapshot):,} chars")

    print(f"\n🤖 Asking Claude to identify {market_config['name']} odds...")

    try:
        market_name, odds_pattern, odds_found, start_marker, end_marker = analyze_snapshot_for_odds(
            snapshot, market_config['prompt']
        )
    except Exception as e:
        print(f"\n❌ Failed to analyze page: {e}")
        return 1

    # Limit to expected number of odds
    expected_count = market_config['selections']
    if len(odds_found) > expected_count:
        odds_found = odds_found[:expected_count]

    print(f"\n✓ Claude found {len(odds_found)} odds values:")
    print(f"   Market: {market_config['name']}")
    print(f"   Format: {odds_pattern}")
    print(f"   Odds: {', '.join(odds_found)}")
    if start_marker:
        print(f"   Start marker: \"{start_marker}\"")
    if end_marker:
        print(f"   End marker: \"{end_marker}\"")

    if len(odds_found) < expected_count:
        print(f"\n⚠️  Expected {expected_count} odds but found {len(odds_found)}")
        print(f"   The page might not have loaded fully (try --wait 10)")

    if not odds_found:
        print(f"\n❌ No odds found. The page might:")
        print(f"   • Not have loaded yet (try --wait 10)")
        print(f"   • Be geo-blocked or require login")
        return 1

    # Learn the pattern from the snapshot
    print(f"\n🧠 Learning pattern from snapshot...")
    name_position, name_separator, extra_info = learn_pattern_from_snapshot(
        snapshot, odds_found, odds_pattern
    )
    print(f"   Detected: names are '{name_position}' odds")

    # Create the rule with market type info and context markers
    rule = SmartRule(
        market_name=market_config['name'],
        odds_pattern=odds_pattern,
        name_position=name_position,
        name_separator=name_separator,
        sample_odds=odds_found,
        description=f"Market: {args.market}",
        group_size=expected_count,
        market_type=args.market,
        selection_labels=market_config['labels'],
        start_marker=start_marker,
        end_marker=end_marker,
    )

    # Test extraction
    print(f"\n🧪 Testing extraction...")
    market = extract_with_smart_rule(snapshot, rule)
    
    if market.selections:
        print(f"   ✓ Extracted {len(market.selections)} selections:")
        for sel in market.selections:
            print(f"      • {sel.name}: {sel.odds}")
    else:
        print(f"   ⚠️  Extraction returned empty - pattern may need adjustment")

    # Save rule (auto-confirm)
    domain = get_domain(args.url)
    rules_path = save_smart_rule(domain, rule, rules_dir=args.rules_dir)

    print(f"\n✅ Setup complete!")
    print(f"   Rules saved to: {rules_path}")
    print(f"\n   Next steps:")
    print(f"   • Scrape:  python main.py scrape --url \"{args.url}\"")

    return 0


def cmd_scrape_smart(args: argparse.Namespace) -> int:
    """Single scrape using learned patterns."""
    from scraper.fetch import fetch_snapshot, get_domain
    from scraper.smart_extract import load_smart_rule, extract_with_smart_rule

    domain = get_domain(args.url)
    
    rule = load_smart_rule(domain, rules_dir=args.rules_dir)
    if not rule:
        print(f"⚠️  No rules found for {domain}. Run setup first:")
        print(f"   python main.py setup --url \"{args.url}\" --prompt \"your request\"")
        return 1

    print(f"🔍 Scraping: {args.url}")
    
    snapshot, _ = fetch_snapshot(args.url, wait_time=args.wait)
    market = extract_with_smart_rule(snapshot, rule)

    print(f"\n✓ {market.market_name}:")
    print("-" * 40)

    for sel in market.selections:
        print(f"  {sel.name}: {sel.odds}")

    # Save to file if output specified
    if hasattr(args, 'output') and args.output:
        data = {
            "market": market.market_name,
            "timestamp": datetime.now().isoformat(),
            "selections": [
                {"name": s.name, "odds": s.odds} 
                for s in market.selections
            ]
        }
        with open(args.output, 'w') as f:
            json.dump(data, f, indent=2)
        print(f"\n📁 Saved to: {args.output}")

    return 0


def cmd_monitor_smart(args: argparse.Namespace) -> int:
    """Continuously monitor odds using learned patterns."""
    from scraper.fetch import fetch_snapshot, get_domain
    from scraper.smart_extract import load_smart_rule, extract_with_smart_rule

    domain = get_domain(args.url)
    
    rule = load_smart_rule(domain, rules_dir=args.rules_dir)
    if not rule:
        print(f"⚠️  No smart rules found for {domain}. Run setup-smart first:")
        print(f"   python main.py setup-smart --url \"{args.url}\" --prompt \"your request\"")
        return 1

    # Setup output file
    if args.output:
        output_path = Path(args.output)
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = Path(f"odds_{domain}_{timestamp}.jsonl")
    
    print(f"📊 Starting odds monitor (SMART MODE)")
    print(f"   URL: {args.url}")
    print(f"   Market: {rule.market_name}")
    print(f"   Pattern: {rule.name_position}")
    print(f"   Interval: {args.interval}s")
    print(f"   Output: {output_path}")
    print(f"   Press Ctrl+C to stop\n")

    iteration = 0
    
    try:
        while args.count is None or iteration < args.count:
            iteration += 1
            
            try:
                snapshot, _ = fetch_snapshot(args.url, wait_time=args.wait)
                market = extract_with_smart_rule(snapshot, rule)
                
                ts = datetime.now().isoformat()
                record = {
                    "timestamp": ts,
                    "market": market.market_name,
                    "selections": [
                        {"name": s.name, "odds": s.odds}
                        for s in market.selections
                    ]
                }
                
                # Write to file
                with open(output_path, "a") as f:
                    f.write(json.dumps(record) + "\n")
                
                # Display summary
                odds_summary = ", ".join(
                    f"{s.name}: {s.odds}" for s in market.selections[:5]
                )
                if len(market.selections) > 5:
                    odds_summary += f" (+{len(market.selections) - 5} more)"
                
                print(f"[{iteration}] {ts[:19]} | {odds_summary}")
                
            except Exception as e:
                logger.error("Scrape failed: %s", e)
                print(f"[{iteration}] ERROR: {e}")

            if args.count is None or iteration < args.count:
                time.sleep(args.interval)

    except KeyboardInterrupt:
        print(f"\n\n✓ Monitoring stopped. {iteration} records saved to {output_path}")

    return 0


def cmd_list(args: argparse.Namespace) -> int:
    """List saved rules."""
    rules_dir = Path(args.rules_dir)
    
    if not rules_dir.exists():
        print("No rules directory found.")
        print("\nTo setup a new site:")
        print('  python main.py setup --url "URL" --prompt "what to extract"')
        return 0

    rule_files = list(rules_dir.glob("*_selectors.json"))
    
    if not rule_files:
        print("No saved rules found.")
        print("\nTo setup a new site:")
        print('  python main.py setup --url "URL" --prompt "what to extract"')
        return 0

    print(f"Saved rules ({len(rule_files)}):\n")
    for rule_file in rule_files:
        domain = rule_file.stem.replace("_selectors", "")
        with open(rule_file) as f:
            rule_data = json.load(f)
        print(f"  • {domain}")
        print(f"    Market: {rule_data.get('market_name', 'Unknown')}")
        print(f"    Selector: {rule_data.get('selection_selector', 'N/A')}")
        print()

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sports betting odds scraper",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Market types:
  h2h_3way    Match Result (Home/Draw/Away)
  h2h_2way    Moneyline (Home/Away, no draw)
  over_under  Over/Under Total Goals
  btts        Both Teams To Score
"""
    )
    parser.add_argument(
        "--rules-dir",
        default="rules",
        help="Directory for selector rules (default: rules/)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # Setup command (uses smart pattern learning)
    setup_parser = subparsers.add_parser(
        "setup",
        help="Analyze page with Claude and learn extraction patterns (one-time per site)"
    )
    setup_parser.add_argument("--url", required=True, help="Event page URL")
    setup_parser.add_argument(
        "--market", "-m",
        required=True,
        choices=["h2h_3way", "h2h_2way", "over_under", "btts"],
        help="Market type to extract (h2h_3way, h2h_2way, over_under, btts)"
    )
    setup_parser.add_argument(
        "--wait", "-w",
        type=float,
        default=5.0,
        help="Seconds to wait for JS to load (default: 5)"
    )
    setup_parser.set_defaults(func=cmd_setup_smart)

    # Scrape command (uses learned patterns)
    scrape_parser = subparsers.add_parser(
        "scrape",
        help="Extract odds using learned patterns (no Claude needed)"
    )
    scrape_parser.add_argument("--url", required=True, help="URL to scrape")
    scrape_parser.add_argument(
        "--wait", "-w",
        type=float,
        default=3.0,
        help="Seconds to wait for JS to load (default: 3)"
    )
    scrape_parser.add_argument(
        "--output", "-o",
        help="Output JSON file (default: auto-generated)"
    )
    scrape_parser.set_defaults(func=cmd_scrape_smart)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
