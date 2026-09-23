# Sports Odds Scraper

An AI-powered Python package that dynamically learns how to extract sports
odds from live bookmaker and sportsbook websites.

Give it a URL and a market—such as `moneyline`, `match winner`, or
`over/under`. GPT-5.6 Luna analyzes the rendered website, generates a
page-specific extractor, tests it against the page, and retries when the
extractor is invalid or incomplete. Once validated, the package monitors the
page and calls your callback with a full snapshot whenever an event is added,
removed, or its odds change.

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
MutationObserver detects live page changes, with a one-second polling fallback
    ↓
Your callback receives every event and its odds, with a UTC scrape timestamp
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
import asyncio
from sports_odds_scraper import OddsMonitor, OddsSnapshot

async def handle_odds(snapshot: OddsSnapshot) -> None:
    print(snapshot.scraped_at)  # timezone-aware UTC datetime
    for event in snapshot.events:
        print(event.event)
        for selection in event.selections:
            print(f"  {selection.name}: {selection.odds} (since {selection.last_changed_at})")

monitor = OddsMonitor(
    url="https://www.pinnacle.com/en/tennis/matchups/live/",
    market="moneyline",
    api_key="your-openai-api-key",
    odds_format="decimal",  # or "fraction"
    poll_interval=1.0,  # seconds between fallback page checks
)

asyncio.run(monitor.run(on_snapshot=handle_odds))
```

`OddsMonitor` requires the API key explicitly. The package does not print
odds or otherwise format application output; it delivers one typed
`OddsSnapshot` through the callback when the extracted odds change. The first
callback contains all available events, and every subsequent callback includes
all currently available events, not only the changed one. `scraped_at` is a
timezone-aware UTC `datetime` recorded when the page snapshot is processed.
Each `OddsEvent` in `snapshot.events` contains the game's selections. If all
events disappear, the callback receives a snapshot with an empty `events` tuple.
`run` and `on_snapshot` are async. Callbacks are scheduled as separate tasks,
so awaited I/O in one callback does not stop the monitor from receiving later
snapshots. Callbacks can overlap and may finish out of order; blocking calls
inside them still block the event loop. Pending callbacks are cancelled when
the monitor stops, and callback failures are logged.
The monitor also checks the rendered page every `poll_interval` seconds (one
second by default). Observer updates and polls share the same change detection,
so an unchanged poll does not trigger another callback. Polling reads the
existing page; it does not make another request to the bookmaker.

Each `Selection` also has `last_changed_at`, a timezone-aware UTC `datetime`.
It is set to `scraped_at` when the selection is first seen, and changes only
when its numeric odds change. An unchanged selection carries its previous
timestamp into the next full snapshot. If a selection disappears and later
returns, it is treated as newly seen. This is the time the scraper observed the
price, not the bookmaker's exact update time.

## Local example

```bash
export OPENAI_API_KEY="your-openai-api-key"
python test_live_scraper.py \
  "https://www.pinnacle.com/en/tennis/matchups/live/" \
  --market moneyline
```

The local runner supplies a printing callback so the package can be tested
directly from a terminal.

Use fractional output when needed:

```bash
python test_live_scraper.py URL --market moneyline --odds-format fraction
```

Internally, all odds are normalized to decimal values. Fractional inputs are
converted using `decimal = fractional + 1`; callbacks receive both numeric
`selection.odds` and formatted `selection.formatted_odds` values.

## Package layout

```text
src/sports_odds_scraper/
├── __init__.py       # Public API
├── client.py         # OddsMonitor implementation
├── models.py         # OddsSnapshot, OddsEvent, and Selection models
└── exceptions.py     # Public exception types
```

## Important limitations

- Website layouts and anti-bot systems can change.
- Generated code is executed locally after import and output checks; review
  your security model before using untrusted URLs.
- Validation improves reliability but cannot mathematically guarantee that a
  website has exposed every market or event.
