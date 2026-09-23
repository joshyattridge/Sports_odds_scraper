# Sports Odds Scraper

An AI-powered Python package that dynamically learns how to extract sports
odds from live bookmaker and sportsbook websites.

Give it a URL and a market—such as `moneyline`, `match winner`, or
`over/under`. GPT-5.6 Luna analyzes the rendered website, generates a
page-specific extractor, tests it against the page, and retries when the
extractor is invalid or incomplete. Once validated, the package monitors the
page and calls your callback whenever an event is new or its odds change.

## How it works

```text
URL + market
    ↓
Playwright renders the website
    ↓
GPT-5.6 Luna generates a dedicated extractor
    ↓
The extractor is compiled, executed, and audited against the page
    ↓
MutationObserver detects live page changes
    ↓
Your callback receives new or changed odds
```

The model is used during scraper generation and validation, not for every
individual odds tick. The generated extractor is saved locally as
`generated_scraper.py` and ignored by Git.

## Installation

```bash
python -m venv .venv
source .venv/bin/activate
pip install .
python -m playwright install chromium
```

For development dependencies:

```bash
pip install ".[dev]"
```

## Library usage

```python
from sports_odds_scraper import OddsMonitor, OddsEvent

def handle_odds(change: OddsEvent) -> None:
    print(change.event)
    for selection in change.selections:
        print(f"  {selection.name}: {selection.odds}")

monitor = OddsMonitor(
    url="https://www.pinnacle.com/en/tennis/matchups/live/",
    market="moneyline",
    api_key="your-openai-api-key",
)

monitor.run(on_change=handle_odds)
```

`OddsMonitor` requires the API key explicitly. The package does not print
odds or otherwise format application output; it delivers typed `OddsEvent`
objects through the callback.

## Local example

```bash
export OPENAI_API_KEY="your-openai-api-key"
python test_live_scraper.py \
  "https://www.pinnacle.com/en/tennis/matchups/live/" \
  --market moneyline
```

The local runner supplies a printing callback so the package can be tested
directly from a terminal.

## Package layout

```text
src/sports_odds_scraper/
├── __init__.py       # Public API
├── client.py         # OddsMonitor implementation
├── models.py         # OddsEvent and Selection models
└── exceptions.py     # Public exception types
```

## Important limitations

- Website layouts and anti-bot systems can change.
- Generated code is executed locally after import and output checks; review
  your security model before using untrusted URLs.
- Validation improves reliability but cannot mathematically guarantee that a
  website has exposed every market or event.
