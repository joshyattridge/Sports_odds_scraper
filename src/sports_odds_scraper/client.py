"""Generate, validate, and run a page-specific odds scraper with a supplied model."""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import logging
import re
import sys
import time
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from urllib.parse import urlparse
from typing import Awaitable, Callable

from playwright.async_api import Error as PlaywrightError, async_playwright

from .exceptions import ScraperGenerationError
from .models import OddsEvent, OddsSnapshot, Selection
from .status import CAPTURE_SCRIPT, parse_snapshot, with_status

GENERATED = Path("generated_scraper.py")
CACHE_DIR = Path.home() / ".cache" / "sports_odds_scraper"
ALLOWED_IMPORTS = {"re", "json", "html", "decimal", "fractions", "typing", "dataclasses"}
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
            if selection.get("status", "unknown") not in {"enabled", "disabled", "unknown"}:
                return False, "each selection needs a valid status"
            control_id = selection.get("control_id")
            if control_id is not None and type(control_id) is not int:
                return False, "control_id must be an integer or null"
            try:
                odds = float(selection["odds"])
            except (KeyError, TypeError, ValueError):
                return False, "each selection needs numeric odds"
            if odds <= 1:
                return False, "odds must be greater than 1"
    return True, f"{len(rows)} valid event(s) for {market}"


async def capture_page(page) -> str:
    """Read text and real odds-control attributes in a single browser snapshot."""
    return json.dumps(await page.evaluate(CAPTURE_SCRIPT), ensure_ascii=False)


def snapshot_for_prompt(snapshot: str, rows: object = None) -> str:
    data = parse_snapshot(snapshot)
    controls = data["controls"]
    if isinstance(rows, list):
        referenced = {
            selection.get("control_id")
            for row in rows if isinstance(row, dict)
            for selection in row.get("selections", []) if isinstance(selection, dict)
        }
        controls = (
            [c for c in controls if isinstance(c, dict) and c.get("id") in referenced]
            + [c for c in controls if isinstance(c, dict) and c.get("id") not in referenced]
        )
    return (
        "RENDERED PAGE TEXT:\n" + str(data.get("text", ""))
        + "\nODDS CONTROL DOM METADATA (id, text, context, attribute hints):\n"
        + json.dumps(controls, ensure_ascii=False)
    )


async def audit_completeness(api_key: str, url: str, market: str, snapshot: str, rows: object, model: str, code: str = "") -> tuple[bool, str]:
    """Audit prices, control mappings, and every status observed on this page."""
    from openai import AsyncOpenAI

    generated_block = f"GENERATED SCRAPER:\n{code}\n" if code else ""
    prompt = f"""Audit a generated sports odds scraper against the rendered page and odds-control DOM evidence.
URL: {url}
REQUESTED MARKET: {market}
EXTRACTED JSON:
{json.dumps(rows, ensure_ascii=False)}
{generated_block}

Compare the events, prices, and each selection's control_id against the page
text AND the DOM controls below. Each status was produced by running the
generated control_status function on the matched control. Confirm at least
one real example for EVERY status (enabled/disabled) present in the extracted
selections. Unknown means that function found insufficient evidence on the
control. If no disabled control appears on this page, leave it out of
verified_statuses.
Ignore other market types and suspended selections that show no price.
Return ONLY JSON:
{{"complete": true, "missing": [], "extra": [], "mismatched": [], "verified_statuses": ["enabled"], "notes": ""}}

Set complete=false if any available target-market event is missing, any odds are
wrong, a selection is linked to the wrong odds control, control_status
contradicts the control's DOM evidence, a clearly identifiable odds control was
left unknown, or another market was included.

PAGE AND DOM EVIDENCE:
{snapshot_for_prompt(snapshot, rows)}"""
    response = await AsyncOpenAI(api_key=api_key).responses.create(model=model, input=prompt)
    try:
        result = json.loads(clean_code(response.output_text))
    except (json.JSONDecodeError, TypeError) as exc:
        return False, f"AI completeness audit returned invalid JSON: {exc}"
    if result.get("complete") is not True:
        return False, "AI completeness audit failed: " + json.dumps(result, ensure_ascii=False)
    observed = {s["status"] for row in rows for s in row["selections"] if s["status"] != "unknown"}
    verified = result.get("verified_statuses", [])
    if not isinstance(verified, list) or not all(isinstance(s, str) for s in verified) or not observed.issubset(set(verified)):
        return False, f"AI did not verify every observed UI status: {sorted(observed)}"
    logger.info("Real-page status validation: verified %s; not observed %s",
                sorted(observed), sorted({"enabled", "disabled"} - observed))
    return True, "complete-page and observed-status audit passed"


def example_cache_path(url: str, market: str) -> Path:
    """Path for the last validated scraper for this site and market. Kept outside the repo."""
    host = (urlparse(url).hostname or "unknown").lower().removeprefix("www.")
    slug = re.sub(r"[^a-z0-9]+", "-", market.lower()).strip("-") or "market"
    return CACHE_DIR / f"{host}__{slug}.py"


def load_scraper_example(url: str, market: str) -> str:
    """Return the stored scraper, including its status function, or an empty string."""
    path = example_cache_path(url, market)
    if not path.is_file():
        return ""
    return path.read_text()


def save_scraper_example(url: str, market: str, code: str) -> None:
    """Store a validated scraper so the next generation can follow it."""
    path = example_cache_path(url, market)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(code)
    logger.info("Stored scraper example for %s", path.name)


async def generate(api_key: str, url: str, market: str, snapshot: str, model: str, feedback: str = "", example: str = "") -> str:
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=api_key)
    example_block = ""
    if example.strip():
        example_block = (
            "A previously validated scraper for this site and market is below. "
            "The live page will not match it exactly. Follow its prepare_actions, "
            "extract, and control_status approach, and adapt them to the current snapshot.\n\n"
            f"PREVIOUSLY VALIDATED SCRAPER:\n{example}\n"
        )
    prompt = f"""Write a complete Python scraper module for this URL and market.
URL: {url}
TARGET MARKET: {market}

The module must define these three functions:
    def prepare_actions() -> list[dict]:
    def extract(snapshot: str) -> list[dict]:
    def control_status(control: dict) -> str:

prepare_actions runs in the browser before validation. Return actions that
remove anything blocking a live odds stream, such as a cookie or consent
banner, an age check, or a modal over the prices. Return [] when nothing is
in the way. Each item is one of:
    {{"action": "click", "role": "button", "name": "Accept all cookies"}}
    {{"action": "click", "selector": "button.accept"}}
    {{"action": "click", "text": "Accept all cookies"}}
    {{"action": "press", "key": "Escape"}}
    {{"action": "wait", "seconds": 1}}
Use at most 8 actions. Validation then watches the rendered odds and rejects
the scraper unless those prices change. The site may deliver updates by
websocket, polling, or any other channel; only the visible odds matter.

extract receives a JSON string with "text" (rendered body text) and "controls"
(odds-control DOM metadata). Parse the JSON. Return:
[{{"event": "event name", "selections": [{{"name": "player/team", "odds": "2.10", "control_id": 12}}]}}]

For each selection, find the matching control by price, text and surrounding
event/market context. Return its id, or null if no unambiguous matching control
exists. Never invent a control id. A fractional button price and its decimal
form are the same price when they agree after rounding to 3 decimal places:
1/150 and 1.007 match. EVS means even money and matches 2.000. Do not reject
a control because the unrounded fraction differs from that decimal.

control_status is called later with one control object, after the library has
matched that control's visible text to the selection price. Return exactly
"enabled", "disabled", or "unknown". Write it from the patterns on THIS page.
Read the control and its ancestor hints — tag, role, class_name, disabled,
aria_disabled, data_state, data_status, data_available, attributes (every
data-* and aria-* name/value), pointer_events, opacity, cursor — plus the
control text and context. Decide how this site shows that a price can be bet,
and how it shows that a price is suspended, locked, or otherwise unavailable.
The same function runs on later snapshots, so base it on those stable DOM
signals. Return "unknown" when that control does not contain enough evidence.
A visible price on its own is not evidence of enabled. Keep the rule general:
do not special-case event names, selection names, or particular prices.

Each control has this shape:
{{"id": 0, "text": "Home 2.10", "context": "Game A", "hints": [{{"tag": "button", "role": null, "href": false, "disabled": false, "aria_disabled": null, "class_name": "price", "data_state": null, "data_status": null, "data_available": null, "attributes": [["data-coupon-state", "open"]], "pointer_events": "auto", "opacity": "1", "cursor": "pointer"}}]}}
hints[0] is the odds control. Later hints are ancestors, nearest first.

Rules:
- Return only the requested market, not handicap, totals, maps, sets, or games.
- Return all odds as decimal strings greater than 1, with 3 decimal places.
  Convert fractional odds such as 5/2 to 3.500 and 1/150 to 1.007. The
  fractions module is allowed.
- Use only the supplied snapshot; do not make network calls.
- Use standard-library imports only.
- Include no CLI, infinite loop, file writes, subprocesses, or explanation.
- Include all requested-market selections with a visible price even if disabled.
- Return [] from extract when the requested market has no visible prices.

{('PREVIOUS VALIDATION FEEDBACK: ' + feedback) if feedback else ''}
{example_block}
RENDERED SNAPSHOT AND CONTROL METADATA:
{snapshot_for_prompt(snapshot)}

Return only Python source code."""
    response = await client.responses.create(model=model, input=prompt)
    return clean_code(response.output_text)


def load_extract(path: Path):
    spec = importlib.util.spec_from_file_location("generated_scraper", path)
    if not spec or not spec.loader:
        raise RuntimeError("could not load generated scraper")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    extract = getattr(module, "extract", None)
    control_status = getattr(module, "control_status", None)
    prepare_actions = getattr(module, "prepare_actions", None)
    if not callable(extract):
        raise RuntimeError("generated scraper has no extract(snapshot) function")
    if not callable(control_status):
        raise RuntimeError("generated scraper has no control_status(control) function")
    if not callable(prepare_actions):
        raise RuntimeError("generated scraper has no prepare_actions() function")
    extract.control_status = control_status
    extract.prepare_actions = prepare_actions
    return extract


_ODDS_TOKEN = re.compile(
    r"\bEVS\b"
    r"|\b\d+(?:[.,]\d+)?\s*/\s*\d+(?:[.,]\d+)?\b"
    r"|\b\d+[.,]\d+\b"
    r"|(?<!\d)[+-]\d{3,}(?!\d)",
    re.IGNORECASE,
)


def odds_in_text(text: str) -> tuple[str, ...]:
    """Odds tokens in one control. Clocks and bare scores are not odds."""
    tokens = []
    for match in _ODDS_TOKEN.finditer(text):
        tokens.append(re.sub(r"\s+", "", match.group(0)).upper().replace(",", "."))
    return tuple(tokens)


def odds_texts(snapshot: str) -> tuple[str, ...]:
    """Sorted odds on visible controls, used to detect a live price change."""
    controls = [control for control in parse_snapshot(snapshot)["controls"] if isinstance(control, dict)]
    tokens = [token for control in controls for token in odds_in_text(str(control.get("text") or ""))]
    return tuple(sorted(tokens))


def feed_status(before: tuple[str, ...], after: tuple[str, ...]) -> tuple[bool, str]:
    """Pass when the rendered odds change, whatever transport delivered them."""
    if not before:
        return False, "no odds controls were visible when the live-feed check started"
    if before != after:
        return True, "visible odds changed during the watch"
    return False, "visible odds did not change during the watch; dismiss any banner or dialog blocking live updates"


def _prepare_locator(page, action: dict):
    if action.get("selector"):
        return page.locator(str(action["selector"]))
    if action.get("role") and action.get("name"):
        return page.get_by_role(str(action["role"]), name=str(action["name"]))
    if action.get("text"):
        return page.get_by_text(str(action["text"]))
    raise RuntimeError("click action needs selector, role and name, or text")


async def apply_prepare(page, actions) -> None:
    """Run the generated actions that unblock a live odds stream."""
    if not isinstance(actions, list) or len(actions) > 8:
        raise RuntimeError("prepare_actions() must return a list of at most 8 actions")
    for index, action in enumerate(actions, start=1):
        if not isinstance(action, dict):
            raise RuntimeError(f"prepare action {index} must be an object")
        kind = action.get("action")
        if kind == "click":
            locator = _prepare_locator(page, action)
            if await locator.count() == 0:
                raise RuntimeError(f"prepare click {index} matched no element: {action}")
            await locator.first.click(timeout=5_000)
            logger.info("Prepare action %d clicked %s", index, action)
        elif kind == "press":
            key = str(action.get("key") or "")
            if not key:
                raise RuntimeError(f"prepare press {index} needs a key")
            await page.keyboard.press(key)
        elif kind == "wait":
            seconds = min(max(float(action.get("seconds", 1)), 0), 5)
            await page.wait_for_timeout(int(seconds * 1000))
        else:
            raise RuntimeError(f"prepare action {index} has unsupported action {kind!r}")


async def confirm_live_updates(page, seconds: float) -> tuple[bool, str]:
    """Watch until the rendered odds change, regardless of how the site delivers them."""
    if seconds <= 0:
        raise ValueError("stream_wait must be greater than 0")
    before = odds_texts(await capture_page(page))
    deadline = time.monotonic() + seconds
    latest = before
    while True:
        ok, reason = feed_status(before, latest)
        if ok:
            logger.info("Live odds feed check passed: %s", reason)
            return True, reason
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            logger.info("Live odds feed check failed: %s", reason)
            return False, reason
        await asyncio.sleep(min(1.0, remaining))
        latest = odds_texts(await capture_page(page))


async def discover(api_key: str, url: str, market: str, page, model: str, retries: int, stream_wait: float = 60.0) -> tuple[Path, object]:
    feedback = ""
    live = False
    example = load_scraper_example(url, market)
    if example:
        logger.info("Including stored scraper example for %s (%s)", urlparse(url).hostname, market)
    for attempt in range(1, retries + 1):
        if attempt > 1 and not live:
            await page.reload(wait_until="domcontentloaded", timeout=45_000)
            await page.wait_for_timeout(3_000)
        snapshot = await capture_page(page)
        logger.info("Generating scraper with %s (attempt %d/%d)", model, attempt, retries)
        code = await generate(api_key, url, market, snapshot, model, feedback, example)
        logger.info("%s returned generated scraper code (%d characters). Validating", model, len(code))
        ok, feedback = safe_code(code)
        if not ok:
            logger.warning("Generated code rejected: %s", feedback)
            continue
        GENERATED.write_text(code)
        try:
            extract = load_extract(GENERATED)
            if not live:
                await apply_prepare(page, extract.prepare_actions())
                ok, feedback = await confirm_live_updates(page, stream_wait)
                if not ok:
                    logger.warning("Generated scraper attempt %d failed: %s", attempt, feedback)
                    continue
                live = True
            snapshot = await capture_page(page)
            rows = with_status(normalise_rows(extract(snapshot)), snapshot, extract.control_status)
            ok, feedback = validate_rows(rows, market)
            if ok:
                logger.info("Local validation passed: %s", feedback)
                ok, audit_feedback = await audit_completeness(api_key, url, market, snapshot, rows, model, code)
                if ok:
                    logger.info("Generated scraper validated on attempt %d: %s", attempt, audit_feedback)
                    save_scraper_example(url, market, code)
                    return GENERATED, extract
                feedback = audit_feedback
        except Exception as exc:
            feedback = f"generated scraper raised {type(exc).__name__}: {exc}"
        logger.warning("Generated scraper attempt %d failed: %s", attempt, feedback)
    raise ScraperGenerationError(f"GPT could not generate a validated scraper: {feedback}")


class SnapshotProcessor:
    """Emit complete odds snapshots when the extracted events or prices change."""

    def __init__(self, extract: Callable[[str], object], market: str, odds_format: str, on_snapshot: Callable[[OddsSnapshot], Awaitable[None]]):
        self.extract = extract
        self.control_status = getattr(extract, "control_status", None)
        self.market = market
        self.odds_format = odds_format
        self.on_snapshot = on_snapshot
        self.previous: dict[str, tuple[tuple[str, str, str], ...]] = {}
        self.last_changed: dict[tuple[str, str], tuple[float, datetime]] = {}
        self.tasks: set[asyncio.Task[None]] = set()

    def _callback_finished(self, task: asyncio.Task[None]) -> None:
        self.tasks.discard(task)
        if not task.cancelled() and (exc := task.exception()) is not None:
            logger.error("Snapshot callback failed", exc_info=(type(exc), exc, exc.__traceback__))

    async def close(self) -> None:
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)

    def process(self, body: str) -> None:
        scraped_at = datetime.now(timezone.utc)
        try:
            rows = with_status(normalise_rows(self.extract(body)), body, self.control_status)
        except Exception as exc:
            logger.warning("Skipped snapshot: generated scraper error: %s", exc)
            return
        # An empty list is a valid live snapshot after all markets disappear.
        # Generation still requires a non-empty result for initial validation.
        ok, message = validate_rows(rows, self.market) if rows != [] else (True, "no active events")
        if not ok:
            logger.warning("Skipped invalid snapshot: %s", message)
            return

        current = {
            str(row["event"]): tuple((str(s["name"]), str(s["odds"]), s["status"]) for s in row["selections"])
            for row in rows
        }
        if current == self.previous:
            return

        last_changed = {}
        events = []
        for row in rows:
            event_name = str(row["event"])
            selections = []
            for selection in row["selections"]:
                name = str(selection["name"])
                odds = float(selection["odds"])
                key = (event_name, name)
                previous = self.last_changed.get(key)
                changed_at = previous[1] if previous is not None and previous[0] == odds else scraped_at
                last_changed[key] = (odds, changed_at)
                selections.append(Selection(
                    name=name,
                    odds=odds,
                    formatted_odds=format_odds(odds, self.odds_format),
                    last_changed_at=changed_at,
                    status=selection["status"],
                ))
            events.append(OddsEvent(event=event_name, selections=tuple(selections)))

        task = asyncio.create_task(self.on_snapshot(OddsSnapshot(scraped_at=scraped_at, events=tuple(events))))
        self.tasks.add(task)
        task.add_done_callback(self._callback_finished)
        self.previous = current
        self.last_changed = last_changed


async def poll_snapshots(page, processor: SnapshotProcessor, interval: float) -> None:
    """Check the rendered page periodically in case a DOM notification was missed."""
    while True:
        await asyncio.sleep(interval)
        try:
            body = await capture_page(page)
        except PlaywrightError as exc:
            logger.warning("Could not poll page snapshot: %s", exc)
            continue
        processor.process(body)


async def run(
    api_key: str,
    url: str,
    market: str,
    on_snapshot: Callable[[OddsSnapshot], Awaitable[None]],
    model: str,
    retries: int = 5,
    wait: float = 30.0,
    odds_format: str = "decimal",
    poll_interval: float = 1.0,
    stream_wait: float = 60.0,
) -> None:
    if not api_key:
        raise ValueError("api_key is required")
    if not model or not str(model).strip():
        raise ValueError("model is required")
    model = str(model).strip()
    if odds_format not in {"decimal", "fraction"}:
        raise ValueError("odds_format must be 'decimal' or 'fraction'")
    if poll_interval <= 0:
        raise ValueError("poll_interval must be greater than 0")
    if stream_wait <= 0:
        raise ValueError("stream_wait must be greater than 0")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(
            viewport={"width": 1920, "height": 1080},
            user_agent=DEFAULT_USER_AGENT,
        )
        processor = None
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            await page.wait_for_timeout(int(wait * 1000))
            _, extract = await discover(api_key, url, market, page, model, retries, stream_wait)
            processor = SnapshotProcessor(extract, market, odds_format, on_snapshot)

            # Observe text and UI-state attributes, including disabled controls.
            await page.expose_binding(
                "odds_changed",
                lambda source, body: processor.process(body),
            )
            await page.evaluate("""() => {
                window.__capture_odds_snapshot = (""" + CAPTURE_SCRIPT + """);
            }""")
            await page.evaluate("""
                () => {
                    let timer = null;
                    const notify = () => {
                        clearTimeout(timer);
                        timer = setTimeout(() => {
                            window.odds_changed(JSON.stringify(window.__capture_odds_snapshot()));
                        }, 250);
                    };
                    window.__odds_observer = new MutationObserver(notify);
                    window.__odds_observer.observe(document.body, {
                        subtree: true,
                        childList: true,
                        characterData: true,
                        attributes: true,
                    });
                    window.__odds_observer_installed = true;
                }
            """)
            processor.process(await capture_page(page))
            logger.info("Watching for live page changes (DOM observer + %.1fs poll)", poll_interval)
            await poll_snapshots(page, processor, poll_interval)
        finally:
            if processor is not None:
                await processor.close()
            await browser.close()


class OddsMonitor:
    """Generate and monitor a live sports-odds scraper for any website.

    Args:
        url: Live odds page URL.
        market: Requested market, such as ``moneyline``.
        api_key: OpenAI API key used for scraper generation and auditing.
        model: Model used for scraper generation and auditing, such as ``gpt-6-luna``.
    """

    def __init__(self, url: str, market: str, api_key: str, *, model: str, odds_format: str = "decimal", retries: int = 5, wait: float = 30.0, poll_interval: float = 1.0, stream_wait: float = 60.0):
        if not api_key:
            raise ValueError("api_key is required")
        if not model or not str(model).strip():
            raise ValueError("model is required")
        self.url = url
        self.market = market
        self.api_key = api_key
        self.model = str(model).strip()
        if odds_format not in {"decimal", "fraction"}:
            raise ValueError("odds_format must be 'decimal' or 'fraction'")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be greater than 0")
        if stream_wait <= 0:
            raise ValueError("stream_wait must be greater than 0")
        self.odds_format = odds_format
        self.retries = retries
        self.wait = wait
        self.poll_interval = poll_interval
        self.stream_wait = stream_wait

    async def run(self, on_snapshot: Callable[[OddsSnapshot], Awaitable[None]]) -> None:
        """Start monitoring and deliver full snapshots when odds change."""
        return await run(self.api_key, self.url, self.market, on_snapshot, self.model, self.retries, self.wait, self.odds_format, self.poll_interval, self.stream_wait)
