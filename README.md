# Sports Odds Scraper

An AI-powered Python package that dynamically learns how to extract sports
odds from live bookmaker and sportsbook websites.

Give it a URL, a market—such as `moneyline`, `match winner`, or
`over/under`—and a model. That model analyzes the rendered website, generates a
page-specific extractor, tests it against the page, and retries when the
extractor is invalid or incomplete. Once validated, the package monitors the
page and calls your callback with a full snapshot whenever an event is added,
removed, or its odds or UI status change.

## How it works

```text
URL + market
    ↓
Playwright captures rendered text and odds-control DOM attributes
    ↓
The supplied model generates an extractor, a status function, and actions that unblock the page
    ↓
Those actions run, then validation requires the visible odds to change
    ↓
MutationObserver detects live page changes, with a one-second polling fallback
    ↓
Your callback receives every event, price, and UI status, with a UTC scrape timestamp
```

The model is used during scraper generation and validation, not for every
individual odds tick. The generated module is saved locally as
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
            print(f"  {selection.name}: {selection.odds} [{selection.status}] "
                  f"(since {selection.last_changed_at})")

monitor = OddsMonitor(
    url="https://www.pinnacle.com/en/tennis/matchups/live/",
    market="moneyline",
    api_key="your-openai-api-key",
    model="gpt-6-luna",
    odds_format="decimal",  # or "fraction"
    poll_interval=1.0,  # seconds between fallback page checks
)

asyncio.run(monitor.run(on_snapshot=handle_odds))
```

`OddsMonitor` requires the API key and model explicitly. The package does not print
odds or otherwise format application output; it delivers one typed
`OddsSnapshot` through the callback when the extracted odds or status change. The first
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
price, not the bookmaker's exact update time. Changing only `status` does not
reset `last_changed_at`.

### Selection status

Every selection has a `status` of `"enabled"`, `"disabled"`, or `"unknown"`.

The supplied model writes a `control_status` function for the page it is looking at.
That function reads the captured odds control and its ancestors: tag, class
names, disabled state, ARIA and data attributes, pointer events, opacity,
cursor, and nearby text. It returns how that site represents an available price
and how it represents a suspended, locked, or otherwise unavailable price. The
library runs this function on the matched control for every snapshot. Because
the rule is generated from the page, a site that marks availability in its own
way can still be interpreted, as long as the signal is present in the captured
control.

- `enabled`: the generated function decided this control can be bet.
- `disabled`: the generated function decided this control is unavailable.
- `unknown`: no control matched the selection's price, or the generated function
  could not tell from the evidence on that control. A visible price by itself
  is not treated as enabled.

The generated module also defines `prepare_actions`. The library runs those
actions in the browser before trusting the page. They are how the scraper
clears a site-specific blocker, such as accepting a cookie banner, so a live
odds channel can start.

After a scraper validates, its source is stored outside the repo at
`~/.cache/sports_odds_scraper/`, one file per site and market. The next
generation for that site includes the stored module in the prompt, including
`control_status`. The model still writes a new scraper for the current page.

During generation, validation watches the rendered odds for up to 60 seconds.
It accepts the scraper when those prices change, whether the site pushes them
over a websocket, polls for them, or uses another channel. A page that loads
one snapshot and then sits still is rejected, and the model is asked to
unblock the updates. Once those prices are moving, a later attempt keeps that
page and only regenerates the extractor. Reloading is reserved for a page
whose odds never started updating. A separate audit then checks the prices, the control
mappings, and **at least one real selection for each enabled or disabled state
that page actually shows**. A state that never appears is left out of the
verified set and reported that way in the logs. When the page's availability
signal is too ambiguous for a reliable rule, those selections stay `unknown`.

A status change emits a full snapshot even when the price is unchanged.
`last_changed_at` still updates only when the numeric odds change. Attribute
changes are watched directly, and the one-second poll checks them again if a
mutation notification was missed.

`status` describes the website's observable UI. It is not a guarantee that a
bookmaker will accept a bet. Only selections with a visible numeric price are
included. A suspended market that removes its prices disappears from the
snapshot. Odds that live only inside an inaccessible iframe or a closed shadow
root are absent from the capture, so their status stays `unknown`.

## Local example

```bash
export OPENAI_API_KEY="your-openai-api-key"
python test_live_scraper.py \
  "https://www.pinnacle.com/en/tennis/matchups/live/" \
  --market moneyline
```

The local runner uses `gpt-6-luna` and supplies a printing callback so the
package can be tested directly from a terminal.

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
├── status.py         # DOM capture; generated code interprets selection status
└── exceptions.py     # Public exception types
```

## Important limitations

- Website layouts and anti-bot systems can change.
- Generated code is executed locally after import and output checks; review
  your security model before using untrusted URLs.
- Validation improves reliability but cannot guarantee that every website
  exposes a usable odds control or an example of each status.
