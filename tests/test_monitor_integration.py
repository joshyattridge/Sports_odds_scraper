"""Exercise the browser observer and polling paths without a live sportsbook."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sports_odds_scraper import OddsMonitor


def extract(body):
    rows = []
    for line in body.splitlines():
        parts = line.strip().split("|")
        if len(parts) == 5:
            event, first, first_odds, second, second_odds = parts
            rows.append({
                "event": event,
                "selections": [
                    {"name": first, "odds": first_odds},
                    {"name": second, "odds": second_odds},
                ],
            })
    return rows


class MonitorBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_observer_and_poll_deliver_complete_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            page_file = Path(directory) / "odds.html"
            page_file.write_text("""<!doctype html><body>
                <div id="a">Game A|Home|2.10|Away|1.80</div>
                <div id="b-old">Game B|Home|3.00|Away|1.50</div>
                <div id="b-new" hidden>Game B|Home|3.40|Away|1.50</div>
            </body>""")
            page_ready = asyncio.Event()
            snapshots = asyncio.Queue()
            page = None

            async def discover_stub(api_key, url, market, browser_page, retries):
                nonlocal page
                page = browser_page
                page_ready.set()
                return Path("unused"), extract

            async def on_snapshot(snapshot):
                await snapshots.put(snapshot)

            monitor = OddsMonitor(page_file.as_uri(), "moneyline", "test-key", wait=0, poll_interval=1)
            with patch("sports_odds_scraper.client.discover", side_effect=discover_stub):
                task = asyncio.create_task(monitor.run(on_snapshot=on_snapshot))
                try:
                    await asyncio.wait_for(page_ready.wait(), 10)
                    initial = await asyncio.wait_for(snapshots.get(), 10)
                    self.assertEqual([event.event for event in initial.events], ["Game A", "Game B"])
                    self.assertTrue(all(
                        selection.last_changed_at == initial.scraped_at
                        for event in initial.events for selection in event.selections
                    ))

                    # Text replacement is picked up by MutationObserver.
                    await page.evaluate("""() => {
                        window.__notifications = 0;
                        const notify = window.odds_changed;
                        window.odds_changed = (...args) => {
                            window.__notifications += 1;
                            return notify(...args);
                        };
                    }""")
                    await page.evaluate("document.querySelector('#a').textContent = 'Game A|Home|2.20|Away|1.80'")
                    changed = await asyncio.wait_for(snapshots.get(), 10)
                    self.assertEqual(await page.evaluate("window.__notifications"), 1)
                    self.assertEqual(len(changed.events), 2)
                    self.assertEqual(changed.events[0].selections[0].odds, 2.20)
                    self.assertEqual(changed.events[0].selections[0].last_changed_at, changed.scraped_at)
                    self.assertEqual(changed.events[0].selections[1].last_changed_at, initial.scraped_at)
                    self.assertEqual(changed.events[1].selections[0].last_changed_at, initial.scraped_at)

                    # Attribute-only visibility changes are outside the observer's
                    # configuration; the page-text poll finds the new visible odds.
                    await page.evaluate("""() => {
                        document.querySelector('#b-old').hidden = true;
                        document.querySelector('#b-new').hidden = false;
                    }""")
                    polled = await asyncio.wait_for(snapshots.get(), 10)
                    self.assertEqual(await page.evaluate("window.__notifications"), 1)
                    self.assertEqual([event.event for event in polled.events], ["Game A", "Game B"])
                    self.assertEqual(polled.events[1].selections[0].odds, 3.40)
                    self.assertEqual(polled.events[1].selections[0].last_changed_at, polled.scraped_at)
                    self.assertEqual(polled.events[0].selections[0].last_changed_at, changed.scraped_at)

                    # A following unchanged poll must not send another callback.
                    with self.assertRaises(asyncio.TimeoutError):
                        await asyncio.wait_for(snapshots.get(), 1.4)
                finally:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass


if __name__ == "__main__":
    unittest.main()
