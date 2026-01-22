#!/usr/bin/env python3
"""Debug AutoScraper matching issue."""

from autoscraper import AutoScraper
from scraper.fetch import fetch_with_browser

def main():
    url = 'https://sports.williamhill.com/betting/en-gb/football/OB_EV38049771/fenerbahce-v-aston-villa'
    
    print("Fetching page with browser...")
    html = fetch_with_browser(url, wait_time=8)
    print(f"HTML length: {len(html)} chars")
    
    # Check if strings exist in raw HTML
    test_strings = ['Fenerbahce', 'Draw', 'Aston Villa', '7/5', '23/10', '9/5']
    print('\nChecking for strings in HTML:')
    for s in test_strings:
        if s in html:
            print(f'  ✓ Found: {s}')
        else:
            print(f'  ✗ Missing: {s}')
    
    # Try building with AutoScraper using HTML parameter
    print('\n--- Test 1: AutoScraper with odds only ---')
    scraper1 = AutoScraper()
    result1 = scraper1.build(url, ['7/5', '23/10', '9/5'], html=html)
    print(f'Result: {result1}')
    print(f'Rules: {scraper1.get_result_exact(url, html=html) if scraper1._all_rules else "No rules"}')
    
    print('\n--- Test 2: AutoScraper with team names ---')
    scraper2 = AutoScraper()
    result2 = scraper2.build(url, ['Fenerbahce', 'Draw', 'Aston Villa'], html=html)
    if result2:
        print(f'Result (first 10): {result2[:10]}')
    else:
        print('Result: None/Empty')
    
    print('\n--- Test 3: Single value "7/5" ---')
    scraper3 = AutoScraper()
    result3 = scraper3.build(url, ['7/5'], html=html)
    print(f'Result: {result3}')
    
    print('\n--- Test 4: Check HTML around the odds ---')
    # Find context around "7/5"
    idx = html.find('>7/5<')
    if idx > 0:
        print(f'Found ">7/5<" at position {idx}')
        print(f'Context: ...{html[idx-100:idx+100]}...')
    else:
        idx = html.find('7/5')
        if idx > 0:
            print(f'Found "7/5" at position {idx}')
            print(f'Context: ...{html[idx-50:idx+50]}...')

if __name__ == '__main__':
    main()
