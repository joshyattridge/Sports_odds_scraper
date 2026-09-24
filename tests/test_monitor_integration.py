"""Exercise the browser observer and polling paths without a live sportsbook."""

import asyncio
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sports_odds_scraper import OddsMonitor
from sports_odds_scraper.status import parse_snapshot


def extract(body):
    events = {}
    for control in parse_snapshot(body)["controls"]:
        if control["hints"][0]["tag"] != "button":
            continue
        event = re.search(r"Game [AB]", control["context"])
        if event is None:
            continue
        name, odds = control["text"].split()
        events.setdefault(event.group(0), []).append({"name": name, "odds": odds, "control_id": control["id"]})
    return [{"event": name, "selections": selections} for name, selections in events.items()]


def control_status(control):
    """Page-specific rule, standing in for the function GPT would generate."""
    for hint in control.get("hints") or []:
        if hint.get("disabled"):
            return "disabled"
        for name, value in hint.get("attributes") or []:
            if name == "data-coupon-state" and value == "frozen":
                return "disabled"
    if (control.get("hints") or [{}])[0].get("tag") == "button":
        return "enabled"
    return "unknown"


extract.control_status = control_status


class MonitorBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_observer_and_poll_deliver_complete_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            page_file = Path(directory) / "odds.html"
            page_file.write_text("""<!doctype html><body>
                <section><h2>Game A</h2>
                    <button id="a-home">Home 2.10</button><button>Away 1.80</button>
                </section>
                <section id="b-old"><h2>Game B</h2>
                    <button>Home 3.00</button><button id="b-away">Away 1.50</button>
                </section>
                <section id="b-new" hidden><h2>Game B</h2>
                    <button>Home 3.40</button><button>Away 1.50</button>
                </section>
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
                        selection.status == "enabled"
                        for event in initial.events for selection in event.selections
                    ))
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
                    await page.evaluate("document.querySelector('#a-home').textContent = 'Home 2.20'")
                    changed = await asyncio.wait_for(snapshots.get(), 10)
                    self.assertEqual(await page.evaluate("window.__notifications"), 1)
                    self.assertEqual(len(changed.events), 2)
                    self.assertEqual(changed.events[0].selections[0].odds, 2.20)
                    self.assertEqual(changed.events[0].selections[0].last_changed_at, changed.scraped_at)
                    self.assertEqual(changed.events[0].selections[1].last_changed_at, initial.scraped_at)
                    self.assertEqual(changed.events[1].selections[0].last_changed_at, initial.scraped_at)

                    # An attribute-only change fires the observer even though the
                    # price stays constant and must not reset its price timestamp.
                    await page.evaluate("document.querySelector('#b-away').disabled = true")
                    suspended = await asyncio.wait_for(snapshots.get(), 10)
                    self.assertEqual(await page.evaluate("window.__notifications"), 2)
                    self.assertEqual(suspended.events[1].selections[1].status, "disabled")
                    self.assertEqual(suspended.events[1].selections[1].odds, 1.50)
                    self.assertEqual(suspended.events[1].selections[1].last_changed_at, initial.scraped_at)

                    # A site-specific attribute is interpreted by the generated
                    # status function. The price timestamp stays put.
                    await page.evaluate("""() => {
                        document.querySelector('#a-home').setAttribute('data-coupon-state', 'frozen');
                    }""")
                    frozen = await asyncio.wait_for(snapshots.get(), 10)
                    self.assertEqual(await page.evaluate("window.__notifications"), 3)
                    self.assertEqual(frozen.events[0].selections[0].status, "disabled")
                    self.assertEqual(frozen.events[0].selections[0].odds, 2.20)
                    self.assertEqual(frozen.events[0].selections[0].last_changed_at, changed.scraped_at)

                    # Simulate a missed observer notification. Polling still finds
                    # the next visible price and retains the other price timestamps.
                    await page.evaluate("""() => {
                        window.__odds_observer.disconnect();
                        document.querySelector('#b-old').hidden = true;
                        document.querySelector('#b-new').hidden = false;
                    }""")
                    polled = await asyncio.wait_for(snapshots.get(), 10)
                    self.assertEqual(await page.evaluate("window.__notifications"), 3)
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
