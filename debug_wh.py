#!/usr/bin/env python3
"""Debug script to inspect William Hill page structure."""

from scraper.fetch import fetch_with_browser
from bs4 import BeautifulSoup
import re

html = fetch_with_browser(
    'https://sports.williamhill.com/betting/en-gb/football/OB_EV38049771/fenerbahce-v-aston-villa',
    wait_time=8
)
soup = BeautifulSoup(html, 'html.parser')

# Find all market wrappers
markets = soup.find_all(class_='btmarket__wrapper')
print(f"Found {len(markets)} markets\n")

for market in markets[:15]:
    # Get market name from header
    header = market.find(class_='btmarket__name') or market.find('h3') or market.find('h4')
    name = header.get_text(strip=True) if header else "Unknown"
    
    # Get selections
    selections = market.find_all(class_='btmarket__selection')
    if selections:
        print(f"Market: {name}")
        for sel in selections[:5]:
            text = sel.get_text(separator=' ', strip=True)
            # Extract name and odds
            odds_elem = sel.find(class_='betbutton__odds')
            odds = odds_elem.get_text(strip=True) if odds_elem else "?"
            # Remove odds from text to get name
            sel_name = re.sub(r'\s*(Was\s*)?\d+/\d+\s*', '', text).strip()
            print(f"  - {sel_name}: {odds}")
        print()
