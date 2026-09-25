import asyncio
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from sports_odds_scraper import OddsMonitor
from sports_odds_scraper.client import SnapshotProcessor, poll_snapshots
from sports_odds_scraper.models import OddsSnapshot
from sports_odds_scraper.status import parse_snapshot


def row(event, first, second):
    return {
        "event": event,
        "selections": [
            {"name": "Home", "odds": first},
            {"name": "Away", "odds": second},
        ],
    }


class SnapshotProcessorTests(unittest.IsolatedAsyncioTestCase):
    async def test_monitor_accepts_on_snapshot_keyword(self):
        async def callback(snapshot):
            pass

        with self.assertRaisesRegex(ValueError, "model is required"):
            OddsMonitor("https://example.com", "moneyline", "test-key", model="")

        monitor = OddsMonitor("https://example.com", "moneyline", "test-key", model="gpt-6-luna")

        with patch("sports_odds_scraper.client.run", new_callable=AsyncMock) as run:
            await monitor.run(on_snapshot=callback)

        run.assert_awaited_once_with("test-key", "https://example.com", "moneyline", callback, "gpt-6-luna", 5, 30.0, "decimal", 1.0, 60.0, 300.0)

    async def test_poll_finds_missed_change_without_duplicate_observer_callback(self):
        snapshots = {
            "initial": [row("Game A", "2.10", "1.80"), row("Game B", "3.00", "1.50")],
            "changed": [row("Game A", "2.20", "1.75"), row("Game B", "3.00", "1.50")],
        }
        received = []

        async def on_snapshot(snapshot):
            received.append(snapshot)

        class Page:
            def __init__(self):
                self.bodies = iter(["initial", "changed"])

            async def evaluate(self, script):
                body = next(self.bodies, None)
                if body is None:
                    raise asyncio.CancelledError
                return {"text": body, "controls": []}

        processor = SnapshotProcessor(lambda body: snapshots[parse_snapshot(body)["text"]], "moneyline", "decimal", on_snapshot)
        try:
            processor.process("initial")  # Observer already detected the initial odds.
            with self.assertRaises(asyncio.CancelledError):
                await poll_snapshots(Page(), processor, 0)
            await asyncio.sleep(0)

            self.assertEqual(len(received), 2)
            self.assertEqual([event.event for event in received[1].events], ["Game A", "Game B"])
            self.assertEqual(received[1].events[0].selections[0].odds, 2.20)
        finally:
            await processor.close()

    async def test_callbacks_contain_all_games_and_scrape_time(self):
        snapshots = {
            "initial": [row("Game A", "2.10", "1.80"), row("Game B", "3.00", "1.50")],
            "changed": [row("Game A", "2.20", "1.75"), row("Game B", "3.00", "1.50")],
            "removed": [row("Game B", "3.00", "1.50")],
            "empty": [],
            "invalid": [row("Broken", "0", "1.50")],
        }
        received = []

        async def on_snapshot(snapshot):
            received.append(snapshot)

        processor = SnapshotProcessor(snapshots.__getitem__, "moneyline", "decimal", on_snapshot)

        before = datetime.now(timezone.utc)
        processor.process("initial")
        processor.process("initial")
        processor.process("invalid")
        processor.process("changed")
        processor.process("removed")
        processor.process("empty")
        processor.process("empty")
        after = datetime.now(timezone.utc)
        await asyncio.sleep(0)

        self.assertEqual(len(received), 4)
        self.assertTrue(all(isinstance(snapshot, OddsSnapshot) for snapshot in received))
        self.assertEqual([[event.event for event in snapshot.events] for snapshot in received], [
            ["Game A", "Game B"], ["Game A", "Game B"], ["Game B"], [],
        ])
        self.assertEqual(received[1].events[0].selections[0].odds, 2.20)
        self.assertEqual(received[1].events[1].selections[0].formatted_odds, "3.000")
        self.assertEqual(received[1].events[0].selections[0].status, "unknown")
        self.assertEqual(received[0].events[0].selections[0].last_changed_at, received[0].scraped_at)
        self.assertEqual(received[1].events[0].selections[0].last_changed_at, received[1].scraped_at)
        self.assertEqual(received[1].events[0].selections[1].last_changed_at, received[1].scraped_at)
        self.assertEqual(received[1].events[1].selections[0].last_changed_at, received[0].scraped_at)
        self.assertTrue(all(before <= snapshot.scraped_at <= after for snapshot in received))
        self.assertTrue(all(snapshot.scraped_at.tzinfo == timezone.utc for snapshot in received))
        await processor.close()

    async def test_last_changed_tracks_numeric_price_and_resets_after_removal(self):
        times = [datetime(2026, 9, 23, 12, 0, second, tzinfo=timezone.utc) for second in range(5)]
        snapshots = {
            "initial": [row("Game A", "2.10", "1.80"), row("Game B", "3.00", "1.50")],
            "reformatted": [row("Game A", "2.1", "1.80"), row("Game B", "3.00", "1.50")],
            "changed": [row("Game A", "2.20", "1.80"), row("Game B", "3.00", "1.50")],
            "removed": [row("Game B", "3.00", "1.50")],
            "returned": [row("Game A", "2.20", "1.80"), row("Game B", "3.00", "1.50")],
        }
        received = []

        async def on_snapshot(snapshot):
            received.append(snapshot)

        processor = SnapshotProcessor(snapshots.__getitem__, "moneyline", "decimal", on_snapshot)
        try:
            with patch("sports_odds_scraper.client.datetime") as clock:
                clock.now.side_effect = times
                for body in snapshots:
                    processor.process(body)
            await asyncio.sleep(0)

            self.assertEqual(len(received), 5)
            self.assertEqual(received[1].events[0].selections[0].last_changed_at, times[0])
            self.assertEqual(received[2].events[0].selections[0].last_changed_at, times[2])
            self.assertEqual(received[2].events[0].selections[1].last_changed_at, times[0])
            self.assertEqual(received[4].events[0].selections[0].last_changed_at, times[4])
            self.assertEqual(received[4].events[1].selections[0].last_changed_at, times[0])
        finally:
            await processor.close()

    async def test_fractional_output_keeps_numeric_decimal_odds(self):
        received = []

        async def on_snapshot(snapshot):
            received.append(snapshot)

        processor = SnapshotProcessor(lambda body: [row("Game A", "5/2", "2.00")], "moneyline", "fraction", on_snapshot)

        processor.process("page")
        await asyncio.sleep(0)

        self.assertEqual(received[0].events[0].selections[0].odds, 3.5)
        self.assertEqual(received[0].events[0].selections[0].formatted_odds, "5/2")
        await processor.close()

    async def test_slow_callback_does_not_delay_later_snapshots(self):
        release_first = asyncio.Event()
        first_started = asyncio.Event()
        received = []

        async def on_snapshot(snapshot):
            if snapshot.events[0].event == "Game A":
                first_started.set()
                await release_first.wait()
            received.append(snapshot.events[0].event)

        processor = SnapshotProcessor(
            lambda body: [row(body, "2.10", "1.80")], "moneyline", "decimal", on_snapshot
        )
        try:
            processor.process("Game A")
            await first_started.wait()
            processor.process("Game B")
            await asyncio.sleep(0)

            self.assertEqual(received, ["Game B"])
            release_first.set()
            await asyncio.sleep(0)
            self.assertEqual(received, ["Game B", "Game A"])
        finally:
            await processor.close()


if __name__ == "__main__":
    unittest.main()
