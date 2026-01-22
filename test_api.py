"""Test capturing API responses from betting sites."""
from playwright.sync_api import sync_playwright
import json

captured = []
all_urls = []

def handle_response(response):
    url = response.url
    all_urls.append(url)
    try:
        content_type = response.headers.get('content-type', '')
        # Capture JSON or any API-like responses
        if response.ok and any(x in content_type for x in ['json', 'text/plain']):
            body = response.body()
            if len(body) > 100:  # Any meaningful responses
                captured.append({
                    'url': url,
                    'content_type': content_type,
                    'status': response.status,
                    'size': len(body),
                    'body': body[:20000]  # First 20KB
                })
    except Exception as e:
        pass

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)  # Headless mode
    page = browser.new_page()
    page.on('response', handle_response)
    
    print('Loading Betfair...')
    page.goto('https://www.betfair.com/sport/football', wait_until='networkidle')
    page.wait_for_timeout(5000)  # Shorter wait
    
    browser.close()

output = []
output.append(f'\n=== Total network requests: {len(all_urls)} ===')
output.append(f'=== Found {len(captured)} JSON/API responses ===\n')

# Show API endpoints found
for c in captured[:20]:
    output.append(f"{c['status']} | {c['content_type'][:30]:30} | {c['size']:>7} bytes | {c['url'][:80]}")

# Find the market prices API
market_prices = [c for c in captured if 'getMarketPrices' in c['url']]
graphql = [c for c in captured if 'bff-gql' in c['url']]

if market_prices:
    mp = market_prices[0]
    output.append(f"\n=== MARKET PRICES API ({mp['size']} bytes) ===")
    try:
        data = json.loads(mp['body'])
        # Show structure of first market
        if isinstance(data, list) and len(data) > 0:
            first_market = data[0]
            output.append(f"Total markets: {len(data)}")
            output.append(f"\nFirst market structure:")
            output.append(json.dumps(first_market, indent=2)[:3000])
    except Exception as e:
        output.append(f"Error: {e}")

if graphql:
    # Find largest GraphQL response (likely has event data)
    gql = max(graphql, key=lambda x: x['size'])
    output.append(f"\n=== GRAPHQL API ({gql['size']} bytes) ===")
    output.append(f"URL: {gql['url'][:150]}")
    try:
        data = json.loads(gql['body'])
        output.append(json.dumps(data, indent=2)[:5000])
    except Exception as e:
        output.append(f"Error: {e}")

# Write to file
with open('api_output.txt', 'w') as f:
    f.write('\n'.join(output))

print("Results written to api_output.txt")
