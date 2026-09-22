"""Generate, validate, and run a page-specific odds scraper with GPT-5.6 Luna."""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

MODEL = "gpt-5.6-luna"
GENERATED = Path("generated_scraper.py")
ALLOWED_IMPORTS = {"re", "json", "html", "decimal", "typing", "dataclasses"}


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


def audit_completeness(url: str, market: str, snapshot: str, rows: object) -> tuple[bool, str]:
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
    response = OpenAI().responses.create(model=MODEL, input=prompt)
    try:
        result = json.loads(clean_code(response.output_text))
    except (json.JSONDecodeError, TypeError) as exc:
        return False, f"AI completeness audit returned invalid JSON: {exc}"
    if result.get("complete") is not True:
        return False, "AI completeness audit failed: " + json.dumps(result, ensure_ascii=False)
    return True, "complete-page audit passed"


def generate(url: str, market: str, snapshot: str, feedback: str = "") -> str:
    from openai import OpenAI

    client = OpenAI()
    prompt = f"""Write a complete Python scraper module for this URL and market.
URL: {url}
TARGET MARKET: {market}

The module must define exactly this function:
    def extract(snapshot: str) -> list[dict]:

It receives rendered body text from the page and must return:
[{{"event": "event name", "selections": [{{"name": "player/team", "odds": "2.10"}}]}}]

Rules:
- Return only the requested market, not handicap, totals, maps, sets, or games.
- Return decimal odds as strings greater than 1.
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


def discover(url: str, market: str, page, retries: int) -> tuple[Path, object]:
    snapshot = page.locator("body").inner_text()
    feedback = ""
    for attempt in range(1, retries + 1):
        print(f"Generating scraper with {MODEL} (attempt {attempt}/{retries})...", flush=True)
        code = generate(url, market, snapshot, feedback)
        print(f"{MODEL} returned generated scraper code ({len(code)} characters). Validating...", flush=True)
        ok, feedback = safe_code(code)
        if not ok:
            print(f"Generated code rejected: {feedback}", file=sys.stderr, flush=True)
            continue
        GENERATED.write_text(code)
        try:
            extract = load_extract(GENERATED)
            rows = extract(snapshot)
            ok, feedback = validate_rows(rows, market)
            if ok:
                print(f"Local validation passed: {feedback}", file=sys.stderr, flush=True)
                ok, audit_feedback = audit_completeness(url, market, snapshot, rows)
                if ok:
                    print(f"Generated scraper validated on attempt {attempt}: {audit_feedback}", file=sys.stderr, flush=True)
                    return GENERATED, extract
                feedback = audit_feedback
        except Exception as exc:
            feedback = f"generated scraper raised {type(exc).__name__}: {exc}"
        print(f"Generated scraper attempt {attempt} failed: {feedback}", file=sys.stderr, flush=True)
    raise RuntimeError(f"GPT could not generate a validated scraper: {feedback}")


def run(url: str, market: str, retries: int, wait: float = 10.0) -> int:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(int(wait * 1000))
            _, extract = discover(url, market, page, retries)
            previous: dict[str, object] = {}

            def process_snapshot(body: str) -> None:
                nonlocal previous
                try:
                    rows = extract(body)
                except Exception as exc:
                    print(f"Skipped snapshot: generated scraper error: {exc}", file=sys.stderr, flush=True)
                    return
                ok, message = validate_rows(rows, market)
                if ok:
                    current = {}
                    for row in rows:
                        event = str(row["event"])
                        values = tuple((str(s["name"]), str(s["odds"])) for s in row["selections"])
                        current[event] = values
                        if previous.get(event) != values:
                            stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
                            print(f"{stamp} | {event} | " + " | ".join(f"{n}: {o}" for n, o in values), flush=True)
                    previous = current
                else:
                    print(f"Skipped invalid snapshot: {message}", file=sys.stderr)

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
            print("Watching for live page changes (event-driven)...", file=sys.stderr, flush=True)
            while True:
                page.wait_for_timeout(1000)
        except KeyboardInterrupt:
            return 0
        finally:
            browser.close()
