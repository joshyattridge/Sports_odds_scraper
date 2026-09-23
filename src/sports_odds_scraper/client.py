"""Generate, validate, and run a page-specific odds scraper with GPT-5.6 Luna."""

from __future__ import annotations

import ast
import importlib.util
import json
import logging
import re
import sys
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Callable

from playwright.sync_api import sync_playwright

from .exceptions import ScraperGenerationError
from .models import OddsEvent, Selection

MODEL = "gpt-5.6-luna"
GENERATED = Path("generated_scraper.py")
ALLOWED_IMPORTS = {"re", "json", "html", "decimal", "typing", "dataclasses"}
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
logger = logging.getLogger(__name__)


def clean_code(text: str) -> str:
    match = re.search(r"```(?:python)?\s*(.*?)```", text, re.I | re.S)
    return (match.group(1) if match else text).strip()


def safe_code(code: str) -> tuple[bool, str]:
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return False, f"SyntaxError: {exc}"
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [x.name.split('.')[0] for x in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module.split('.')[0] if node.module else ""]
        else:
            continue
        forbidden = [name for name in names if name not in ALLOWED_IMPORTS]
        if forbidden:
            return False, f"forbidden imports: {forbidden}"
    return True, "ok"


def normalise_rows(rows: object) -> object:
    """Convert decimal or fractional model output to canonical decimal strings."""
    if not isinstance(rows, list):
        return rows
    normalised = []
    for row in rows:
        if not isinstance(row, dict):
            normalised.append(row)
            continue
        copied = dict(row)
        selections = []
        for selection in row.get("selections", []):
            item = dict(selection)
            value = str(item.get("odds", "")).strip()
            if re.fullmatch(r"\d+(?:\.\d+)?/\d+(?:\.\d+)?", value):
                numerator, denominator = value.split("/")
                # Decimal odds = fractional odds + 1.
                item["odds"] = str(float(Fraction(numerator) / Fraction(denominator) + 1))
            else:
                item["odds"] = value
            selections.append(item)
        copied["selections"] = selections
        normalised.append(copied)
    return normalised


def format_odds(decimal_odds: float, odds_format: str) -> str:
    if odds_format == "decimal":
        return f"{decimal_odds:.3f}"
    fraction = Fraction(decimal_odds - 1).limit_denominator(1000)
    return f"{fraction.numerator}/{fraction.denominator}"


def validate_rows(rows: object, market: str) -> tuple[bool, str]:
    if not isinstance(rows, list) or not rows:
        return False, "extract(snapshot) must return a non-empty list"
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("selections"), list):
            return False, "each result must be {event, selections}"
        if len(row["selections"]) < 2:
            return False, "each event needs at least two selections"
        for selection in row["selections"]:
            if not isinstance(selection, dict) or not selection.get("name"):
                return False, "each selection needs a name"
            try:
                odds = float(selection["odds"])
            except (KeyError, TypeError, ValueError):
                return False, "each selection needs numeric odds"
            if odds <= 1:
                return False, "odds must be greater than 1"
    return True, f"{len(rows)} valid event(s) for {market}"


def audit_completeness(api_key: str, url: str, market: str, snapshot: str, rows: object) -> tuple[bool, str]:
    """Use a separate model pass to check completeness and exact prices."""
    from openai import OpenAI

    prompt = f"""Audit a generated sports odds scraper against the complete rendered page.
URL: {url}
REQUESTED MARKET: {market}
EXTRACTED JSON:
{json.dumps(rows, ensure_ascii=False)}

Compare the extracted events and prices against the entire page text below.
Ignore suspended markets with no prices and ignore every other market type.
Return ONLY JSON:
{{"complete": true, "missing": [], "extra": [], "mismatched": [], "notes": ""}}

Set complete=false if any available target-market event is missing, any odds are
wrong, or another market (maps, sets, handicap, totals) was included.

COMPLETE RENDERED PAGE:
{snapshot[:90000]}"""
    response = OpenAI(api_key=api_key).responses.create(model=MODEL, input=prompt)
    try:
        result = json.loads(clean_code(response.output_text))
    except (json.JSONDecodeError, TypeError) as exc:
        return False, f"AI completeness audit returned invalid JSON: {exc}"
    if result.get("complete") is not True:
        return False, "AI completeness audit failed: " + json.dumps(result, ensure_ascii=False)
    return True, "complete-page audit passed"


def generate(api_key: str, url: str, market: str, snapshot: str, feedback: str = "") -> str:
    from openai import OpenAI

    client = OpenAI(api_key=api_key)
    prompt = f"""Write a complete Python scraper module for this URL and market.
URL: {url}
TARGET MARKET: {market}

The module must define exactly this function:
    def extract(snapshot: str) -> list[dict]:

It receives rendered body text from the page and must return:
[{{"event": "event name", "selections": [{{"name": "player/team", "odds": "2.10"}}]}}]

Rules:
- Return only the requested market, not handicap, totals, maps, sets, or games.
- Return all odds as decimal strings greater than 1. Convert fractional odds
  such as 5/2 to decimal odds such as 3.500.
- Use only the supplied snapshot; do not make network calls.
- Use standard-library imports only.
- Include no CLI, infinite loop, file writes, subprocesses, or explanation.
- Return [] when the market is absent or suspended.

{('PREVIOUS VALIDATION FEEDBACK: ' + feedback) if feedback else ''}

RENDERED SNAPSHOT:
{snapshot[:50000]}

Return only Python source code."""
    response = client.responses.create(model=MODEL, input=prompt)
    return clean_code(response.output_text)


def load_extract(path: Path):
    spec = importlib.util.spec_from_file_location("generated_scraper", path)
    if not spec or not spec.loader:
        raise RuntimeError("could not load generated scraper")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not callable(getattr(module, "extract", None)):
        raise RuntimeError("generated scraper has no extract(snapshot) function")
    return module.extract


def discover(api_key: str, url: str, market: str, page, retries: int) -> tuple[Path, object]:
    snapshot = page.locator("body").inner_text()
    feedback = ""
    for attempt in range(1, retries + 1):
        logger.info("Generating scraper with %s (attempt %d/%d)", MODEL, attempt, retries)
        code = generate(api_key, url, market, snapshot, feedback)
        logger.info("%s returned generated scraper code (%d characters). Validating", MODEL, len(code))
        ok, feedback = safe_code(code)
        if not ok:
            logger.warning("Generated code rejected: %s", feedback)
            continue
        GENERATED.write_text(code)
        try:
            extract = load_extract(GENERATED)
            rows = normalise_rows(extract(snapshot))
            ok, feedback = validate_rows(rows, market)
            if ok:
                logger.info("Local validation passed: %s", feedback)
                ok, audit_feedback = audit_completeness(api_key, url, market, snapshot, rows)
                if ok:
                    logger.info("Generated scraper validated on attempt %d: %s", attempt, audit_feedback)
                    return GENERATED, extract
                feedback = audit_feedback
        except Exception as exc:
            feedback = f"generated scraper raised {type(exc).__name__}: {exc}"
        logger.warning("Generated scraper attempt %d failed: %s", attempt, feedback)
    raise ScraperGenerationError(f"GPT could not generate a validated scraper: {feedback}")


def run(
    api_key: str,
    url: str,
    market: str,
    on_change: Callable[[OddsEvent], None],
    retries: int = 5,
    wait: float = 30.0,
    odds_format: str = "decimal",
) -> int:
    if not api_key:
        raise ValueError("api_key is required")
    if odds_format not in {"decimal", "fraction"}:
        raise ValueError("odds_format must be 'decimal' or 'fraction'")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(
            viewport={"width": 1920, "height": 1080},
            user_agent=DEFAULT_USER_AGENT,
        )
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(int(wait * 1000))
            _, extract = discover(api_key, url, market, page, retries)
            previous: dict[str, object] = {}

            def process_snapshot(body: str) -> None:
                nonlocal previous
                try:
                    rows = normalise_rows(extract(body))
                except Exception as exc:
                    logger.warning("Skipped snapshot: generated scraper error: %s", exc)
                    return
                ok, message = validate_rows(rows, market)
                if ok:
                    current = {}
                    for row in rows:
                        event = str(row["event"])
                        values = tuple((str(s["name"]), str(s["odds"])) for s in row["selections"])
                        current[event] = values
                        if previous.get(event) != values:
                            on_change(OddsEvent(
                                event=event,
                                selections=tuple(
                                    Selection(
                                        name=n,
                                        odds=float(o),
                                        formatted_odds=format_odds(float(o), odds_format),
                                    )
                                    for n, o in values
                                ),
                            ))
                    previous = current
                else:
                    logger.warning("Skipped invalid snapshot: %s", message)

            # Pinnacle updates its React UI from live streams. Observe the
            # rendered DOM and debounce bursts of mutations into one scrape.
            page.expose_binding(
                "odds_changed",
                lambda source, body: process_snapshot(body),
            )
            page.evaluate("""
                () => {
                    let timer = null;
                    const notify = () => {
                        clearTimeout(timer);
                        timer = setTimeout(() => {
                            window.odds_changed(document.body.innerText);
                        }, 250);
                    };
                    new MutationObserver(notify).observe(document.body, {
                        subtree: true,
                        childList: true,
                        characterData: true,
                    });
                    window.__odds_observer_installed = true;
                }
            """)
            process_snapshot(page.locator("body").inner_text())
            logger.info("Watching for live page changes (event-driven)")
            while True:
                page.wait_for_timeout(1000)
        except KeyboardInterrupt:
            return 0
        finally:
            browser.close()


class OddsMonitor:
    """Generate and monitor a live sports-odds scraper for any website.

    Args:
        url: Live odds page URL.
        market: Requested market, such as ``moneyline``.
        api_key: OpenAI API key used for scraper generation and auditing.
    """

    def __init__(self, url: str, market: str, api_key: str, *, odds_format: str = "decimal", retries: int = 5, wait: float = 30.0):
        if not api_key:
            raise ValueError("api_key is required")
        self.url = url
        self.market = market
        self.api_key = api_key
        if odds_format not in {"decimal", "fraction"}:
            raise ValueError("odds_format must be 'decimal' or 'fraction'")
        self.odds_format = odds_format
        self.retries = retries
        self.wait = wait

    def run(self, on_change: Callable[[OddsEvent], None]) -> int:
        """Start monitoring and call ``on_change`` for new or changed events."""
        return run(self.api_key, self.url, self.market, on_change, self.retries, self.wait, self.odds_format)
