import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sports_odds_scraper.client import ALLOWED_IMPORTS, audit_completeness, discover, feed_status, odds_texts, safe_code, validate_rows
from sports_odds_scraper.status import with_status


def page_status(control):
    """Stand-in for the function GPT writes after seeing one page."""
    for hint in control.get("hints") or []:
        if hint.get("disabled") or hint.get("aria_disabled") == "true":
            return "disabled"
        if "suspended" in str(hint.get("class_name") or ""):
            return "disabled"
        for name, value in hint.get("attributes") or []:
            if name == "data-coupon-state" and value == "frozen":
                return "disabled"
    hint = (control.get("hints") or [{}])[0]
    if hint.get("tag") == "button":
        return "enabled"
    return "unknown"


def control(control_id, odds, *hints):
    return {"id": control_id, "text": f"Home {odds}", "context": "Game A", "hints": list(hints)}


class StatusEvidenceTests(unittest.TestCase):
    def test_generated_function_supplies_status(self):
        page = json.dumps({"text": "Game A Home 2.10 Away 1.80", "controls": [
            control(0, "2.10", {"tag": "button", "disabled": False}),
            control(1, "1.80", {"tag": "button"}, {"class_name": "market-suspended"}),
        ]})
        rows = [{"event": "Game A", "selections": [
            {"name": "Home", "odds": "2.10", "control_id": 0, "status": "disabled"},
            {"name": "Away", "odds": "1.80", "control_id": 1, "status": "enabled"},
        ]}]

        enriched = with_status(rows, page, page_status)

        self.assertEqual([s["status"] for s in enriched[0]["selections"]], ["enabled", "disabled"])
        self.assertEqual([s["status"] for s in with_status(rows, page)[0]["selections"]], ["unknown", "unknown"])
        self.assertTrue(validate_rows(enriched, "moneyline")[0])

    def test_generated_function_reads_site_specific_attributes(self):
        page = json.dumps({"text": "Game A Home 2.10 Away 1.80", "controls": [
            control(0, "2.10", {"tag": "button", "attributes": [["data-coupon-state", "open"]]}),
            control(1, "1.80", {"tag": "button", "attributes": [["data-coupon-state", "frozen"]]}),
        ]})
        rows = [{"event": "Game A", "selections": [
            {"name": "Home", "odds": "2.10", "control_id": 0},
            {"name": "Away", "odds": "1.80", "control_id": 1},
        ]}]

        self.assertEqual(
            [s["status"] for s in with_status(rows, page, page_status)[0]["selections"]],
            ["enabled", "disabled"],
        )

    def test_ambiguous_or_wrong_price_is_unknown(self):
        page = json.dumps({"text": "Game A", "controls": [
            control(0, "2.10", {"tag": "div", "class_name": "quote"}),
            control(1, "1.80", {"tag": "button", "aria_disabled": "true"}),
        ]})
        rows = [{"event": "Game A", "selections": [
            {"name": "Home", "odds": "2.10", "control_id": 0},
            {"name": "Away", "odds": "2.10", "control_id": 1},
        ]}]

        self.assertEqual(
            [s["status"] for s in with_status(rows, page, page_status)[0]["selections"]],
            ["unknown", "unknown"],
        )

    def test_rounded_fraction_still_reaches_the_status_function(self):
        page = json.dumps({"text": "Game A", "controls": [
            control(0, "1/150", {"tag": "button", "disabled": True}),
            control(1, "40/1", {"tag": "button", "disabled": False}),
        ]})
        rows = [{"event": "Game A", "selections": [
            {"name": "Home", "odds": "1.007", "control_id": 0},
            {"name": "Away", "odds": "41.000", "control_id": 1},
        ]}]

        self.assertEqual(
            [s["status"] for s in with_status(rows, page, page_status)[0]["selections"]],
            ["disabled", "enabled"],
        )
        self.assertIn("fractions", ALLOWED_IMPORTS)
        self.assertTrue(safe_code("from fractions import Fraction\n")[0])

        evs_page = json.dumps({"text": "Game A", "controls": [
            control(0, "EVS", {"tag": "button", "disabled": False}),
        ]})
        evs_rows = [{"event": "Game A", "selections": [
            {"name": "Home", "odds": "2.000", "control_id": 0},
            {"name": "Away", "odds": "2.000", "control_id": 1},
        ]}]
        self.assertEqual(with_status(evs_rows, evs_page, page_status)[0]["selections"][0]["status"], "enabled")


def scraper_module(selections: str) -> str:
    return f"""import json
def extract(snapshot):
    return [{{'event': 'Game A', 'selections': [{selections}]}}]

def control_status(control):
    return 'enabled'

def prepare_actions():
    return []
"""


class LiveFeedTests(unittest.IsolatedAsyncioTestCase):
    def test_validation_passes_when_rendered_odds_change(self):
        before = ("Away 1.80", "Home 2.10")
        after = ("Away 1.80", "Home 2.20")
        self.assertFalse(feed_status(before, before)[0])
        self.assertTrue(feed_status(before, after)[0])
        self.assertIn("no odds controls", feed_status((), after)[1])

    def test_live_check_ignores_clock_changes(self):
        clock = json.dumps({"text": "", "controls": [
            {"text": "14:32:35 (GMT +01:00)"},
            {"text": "+1.5\n1.212"},
            {"text": "Home 8/11"},
        ]})
        clock_tick = json.dumps({"text": "", "controls": [
            {"text": "14:32:37 (GMT +01:00)"},
            {"text": "+1.5\n1.212"},
            {"text": "Home 8/11"},
        ]})
        price_change = json.dumps({"text": "", "controls": [
            {"text": "14:32:37 (GMT +01:00)"},
            {"text": "+1.5\n1.131"},
            {"text": "Home 8/11"},
        ]})
        self.assertEqual(odds_texts(clock), odds_texts(clock_tick))
        self.assertFalse(feed_status(odds_texts(clock), odds_texts(clock_tick))[0])
        self.assertTrue(feed_status(odds_texts(clock), odds_texts(price_change))[0])
        self.assertIn("no odds controls", feed_status(odds_texts(json.dumps({"text": "", "controls": [{"text": "14:32:35 (GMT +01:00)"}]})), ("1.212",))[1])

    async def test_retry_keeps_a_page_whose_odds_are_already_moving(self):
        page_data = {"text": "Game A Home 2.10 Away 1.80", "controls": [
            control(0, "2.10", {"tag": "button"}),
            control(1, "1.80", {"tag": "button"}),
        ]}
        incomplete = scraper_module("{'name': 'Home', 'odds': '2.10', 'control_id': 0}")
        complete = scraper_module(
            "{'name': 'Home', 'odds': '2.10', 'control_id': 0}, {'name': 'Away', 'odds': '1.80', 'control_id': 1}"
        )
        page = SimpleNamespace(
            evaluate=AsyncMock(return_value=page_data),
            reload=AsyncMock(),
            wait_for_timeout=AsyncMock(),
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "generated_scraper.py"
            with patch("sports_odds_scraper.client.GENERATED", output), \
                 patch("sports_odds_scraper.client.generate", new_callable=AsyncMock, side_effect=[incomplete, complete]), \
                 patch("sports_odds_scraper.client.apply_prepare", new_callable=AsyncMock) as prepare, \
                 patch("sports_odds_scraper.client.confirm_live_updates", new_callable=AsyncMock, return_value=(True, "live feed")), \
                 patch("sports_odds_scraper.client.audit_completeness", new_callable=AsyncMock, return_value=(True, "ok")):
                await discover("test-key", "https://example.com", "moneyline", page, "gpt-6-luna", 2)

        page.reload.assert_not_awaited()
        prepare.assert_awaited_once()

    async def test_retry_reloads_when_odds_never_started_moving(self):
        page_data = {"text": "Game A Home 2.10 Away 1.80", "controls": [
            control(0, "2.10", {"tag": "button"}),
            control(1, "1.80", {"tag": "button"}),
        ]}
        generated = scraper_module(
            "{'name': 'Home', 'odds': '2.10', 'control_id': 0}, {'name': 'Away', 'odds': '1.80', 'control_id': 1}"
        )
        page = SimpleNamespace(
            evaluate=AsyncMock(return_value=page_data),
            reload=AsyncMock(),
            wait_for_timeout=AsyncMock(),
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "generated_scraper.py"
            with patch("sports_odds_scraper.client.GENERATED", output), \
                 patch("sports_odds_scraper.client.generate", new_callable=AsyncMock, return_value=generated), \
                 patch("sports_odds_scraper.client.apply_prepare", new_callable=AsyncMock), \
                 patch("sports_odds_scraper.client.confirm_live_updates", new_callable=AsyncMock, side_effect=[
                     (False, "visible odds did not change during the watch"),
                     (True, "live feed"),
                 ]), \
                 patch("sports_odds_scraper.client.audit_completeness", new_callable=AsyncMock, return_value=(True, "ok")):
                await discover("test-key", "https://example.com", "moneyline", page, "gpt-6-luna", 2)

        page.reload.assert_awaited_once()


class StatusAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_generation_validates_both_live_statuses_before_monitoring(self):
        page_data = {"text": "Game A Home 2.10 Away 1.80", "controls": [
            control(0, "2.10", {"tag": "button"}),
            control(1, "1.80", {"tag": "button", "disabled": True}),
        ]}
        generated = """import json
def extract(snapshot):
    controls = json.loads(snapshot)['controls']
    return [{'event': 'Game A', 'selections': [
        {'name': 'Home', 'odds': '2.10', 'control_id': controls[0]['id']},
        {'name': 'Away', 'odds': '1.80', 'control_id': controls[1]['id']},
    ]}]

def control_status(control):
    for hint in control.get('hints') or []:
        if hint.get('disabled'):
            return 'disabled'
    hint = (control.get('hints') or [{}])[0]
    if hint.get('tag') == 'button':
        return 'enabled'
    return 'unknown'

def prepare_actions():
    return []
"""

        async def check_audit(api_key, url, market, snapshot, rows, model, code=""):
            self.assertEqual({s["status"] for s in rows[0]["selections"]}, {"enabled", "disabled"})
            return True, "observed states verified"

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "generated_scraper.py"
            with patch("sports_odds_scraper.client.GENERATED", output), \
                 patch("sports_odds_scraper.client.generate", new_callable=AsyncMock, return_value=generated), \
                 patch("sports_odds_scraper.client.apply_prepare", new_callable=AsyncMock), \
                 patch("sports_odds_scraper.client.confirm_live_updates", new_callable=AsyncMock, return_value=(True, "live feed")), \
                 patch("sports_odds_scraper.client.audit_completeness", side_effect=check_audit) as audit:
                page = SimpleNamespace(evaluate=AsyncMock(return_value=page_data))
                path, scraper = await discover("test-key", "https://example.com", "moneyline", page, "gpt-6-luna", 1)

            self.assertEqual(path, output)
            self.assertEqual(len(scraper(json.dumps(page_data))[0]["selections"]), 2)
            audit.assert_awaited_once()

    async def test_audit_requires_real_examples_of_every_observed_status(self):
        page = json.dumps({"text": "Game A Home 2.10 Away 1.80", "controls": [
            control(0, "2.10", {"tag": "button"}),
            control(1, "1.80", {"tag": "button", "disabled": True}),
        ]})
        rows = with_status([{"event": "Game A", "selections": [
            {"name": "Home", "odds": "2.10", "control_id": 0},
            {"name": "Away", "odds": "1.80", "control_id": 1},
        ]}], page, page_status)
        response = SimpleNamespace(output_text=json.dumps({"complete": True, "verified_statuses": ["enabled"]}))
        with patch("openai.AsyncOpenAI") as openai:
            openai.return_value.responses.create = AsyncMock(return_value=response)
            passed, reason = await audit_completeness("test-key", "https://example.com", "moneyline", page, rows, "gpt-6-luna")
            self.assertFalse(passed)
            self.assertIn("every observed UI status", reason)

            response.output_text = json.dumps({"complete": True, "verified_statuses": ["enabled", "disabled"]})
            passed, reason = await audit_completeness("test-key", "https://example.com", "moneyline", page, rows, "gpt-6-luna")
            self.assertTrue(passed, reason)


if __name__ == "__main__":
    unittest.main()
