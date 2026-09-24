"""Capture odds-control DOM evidence for a page-specific status function."""

import json
import re


CAPTURE_SCRIPT = r"""() => {
    const body = document.body;
    if (!body) return {text: '', controls: []};
    const controls = [];
    const seen = new Set();
    const price = /\b\d+(?:\.\d+)?(?:\s*\/\s*\d+(?:\.\d+)?)?\b/;
    const actionable = 'button, a[href], [role="button"], [aria-disabled], [disabled], [data-state], [data-status], [class*="odd" i], [class*="price" i], [class*="selection" i]';

    function add(element) {
        // A numeric price span may be inside the actual betting button.
        element = element?.closest('button, a[href], [role="button"]') || element;
        if (!element || seen.has(element)) return;
        const text = (element.innerText || '').trim();
        if (!text || !price.test(text)) return;
        const style = getComputedStyle(element);
        if (!element.getClientRects().length || style.display === 'none' || style.visibility === 'hidden') return;
        seen.add(element);
        const hints = [];
        for (let node = element, depth = 0; node && node !== body.parentElement && depth < 6; node = node.parentElement, depth++) {
            const css = getComputedStyle(node);
            hints.push({
                tag: node.tagName.toLowerCase(),
                role: node.getAttribute('role'),
                href: node.hasAttribute('href'),
                disabled: node.matches(':disabled') || node.hasAttribute('disabled'),
                aria_disabled: node.getAttribute('aria-disabled'),
                class_name: typeof node.className === 'string' ? node.className.slice(0, 250) : '',
                data_state: node.getAttribute('data-state'),
                data_status: node.getAttribute('data-status'),
                data_available: node.getAttribute('data-available'),
                attributes: Array.from(node.attributes)
                    .filter(a => /^(?:data-|aria-)/i.test(a.name))
                    .slice(0, 16)
                    .map(a => [a.name, String(a.value).slice(0, 80)]),
                pointer_events: css.pointerEvents,
                opacity: css.opacity,
                cursor: css.cursor,
            });
        }
        const contexts = [];
        for (let parent = element.parentElement, depth = 0; parent && depth < 5; parent = parent.parentElement, depth++) {
            const context = (parent.innerText || '').trim();
            if (context && !contexts.includes(context)) contexts.push(context);
        }
        controls.push({
            id: controls.length,
            text,
            context: contexts.join(' | '),
            hints,
        });
    }

    // Prefer actionable elements so a price span inside a button inherits its state.
    for (const element of body.querySelectorAll(actionable)) add(element);
    const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) {
        if (!price.test(node.nodeValue || '')) continue;
        const parent = node.parentElement;
        const control = parent?.closest(actionable) || parent;
        add(control);
    }
    return {text: body.innerText, controls};
}"""

def parse_snapshot(snapshot: str) -> dict:
    """Also accept plain text from older extractors and lightweight tests."""
    try:
        data = json.loads(snapshot)
    except (TypeError, json.JSONDecodeError):
        return {"text": snapshot, "controls": []}
    if isinstance(data, dict) and isinstance(data.get("controls"), list):
        return data
    return {"text": snapshot, "controls": []}


def _price_matches(text: str, odds: str) -> bool:
    """Require the referenced control to display the selection's actual price."""
    try:
        target = float(odds)
    except (TypeError, ValueError):
        return False
    # William Hill shows even money as EVS rather than 1/1 or 2.000.
    if re.search(r"\bEVS\b", text, re.I) and abs(target - 2.0) <= 0.0005 + 1e-9:
        return True
    for value in re.findall(r"(?<![\w.])\d+(?:[.,]\d+)?(?:\s*/\s*\d+(?:[.,]\d+)?)?(?![\w.])", text):
        try:
            value = value.replace(",", ".").replace(" ", "")
            if "/" in value:
                numerator, denominator = value.split("/")
                price = float(numerator) / float(denominator) + 1
            else:
                price = float(value)
            # Fractional prices are stored to 3 decimal places, so 1/150 matches 1.007.
            if abs(price - target) <= 0.0005 + 1e-9:
                return True
        except (ValueError, ZeroDivisionError):
            continue
    return False


def with_status(rows: list[dict], snapshot: str, control_status=None) -> list[dict]:
    """Run the generated status function on each price-matched control.

    The library supplies the live control and stores the function's return
    value. A status field already present on the selection is ignored. With no
    generated function, every selection stays unknown.
    """
    controls = {control.get("id"): control for control in parse_snapshot(snapshot)["controls"] if isinstance(control, dict)}
    enriched = []
    for row in rows:
        selections = []
        for selection in row["selections"]:
            control_id = selection.get("control_id")
            control = controls.get(control_id) if type(control_id) is int else None
            if control_status is not None and control and _price_matches(str(control.get("text", "")), str(selection["odds"])):
                status = control_status(control)
            else:
                status = "unknown"
            selections.append({**selection, "status": status})
        enriched.append({**row, "selections": selections})
    return enriched
