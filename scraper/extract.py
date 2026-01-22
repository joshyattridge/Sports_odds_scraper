"""Extract odds using CSS selectors with Playwright snapshot for LLM analysis."""

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from bs4 import BeautifulSoup
import anthropic
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-haiku-4-5-20251001"


@dataclass
class ExtractionRule:
    """A rule for extracting odds from a page."""
    market_name: str
    selection_selector: str  # CSS selector for each selection container
    name_selector: str       # CSS selector for name within selection (relative)
    odds_selector: str       # CSS selector for odds within selection (relative)
    description: str


@dataclass 
class ExtractedSelection:
    """A single extracted selection."""
    name: str
    odds: str


@dataclass
class ExtractedMarket:
    """An extracted market with selections."""
    market_name: str
    selections: List[ExtractedSelection]


ANALYSIS_PROMPT = """You are analyzing a sports betting webpage to find CSS selectors for extracting odds.

USER REQUEST: {user_prompt}

Below is an accessibility snapshot of the page showing the structure and text content.
Analyze it to identify the betting market the user wants.

ACCESSIBILITY SNAPSHOT:
{snapshot}

---

Now here's a SAMPLE of the HTML around the betting content (to help you identify CSS classes):
{html_sample}

---

Based on this, identify:
1. The CSS selector for each betting selection container (button/div with name + odds)
2. Within that container, the selector for the selection name
3. Within that container, the selector for the odds value

Return a JSON object:
{{
  "market_name": "Match Result" or similar,
  "selection_selector": "CSS selector for selection container",
  "name_selector": "selector for name WITHIN selection",
  "odds_selector": "selector for odds WITHIN selection", 
  "description": "Brief explanation"
}}

TIPS:
- Use class selectors (.classname) - they're most reliable
- The selection_selector should match multiple elements (one per outcome)
- Look for patterns like .selection, .outcome, .betbutton, .market

Return ONLY the JSON object:"""


def _extract_html_sample(html_content: str, max_size: int = 20000) -> str:
    """Extract a focused HTML sample around betting content for CSS class reference."""
    from bs4 import BeautifulSoup
    
    soup = BeautifulSoup(html_content, 'html.parser')
    
    # Look for common betting container patterns
    betting_patterns = [
        {'class_': re.compile(r'btmarket|market.*selection', re.I)},
        {'class_': re.compile(r'selection|outcome', re.I)},
        {'class_': re.compile(r'betbutton|bet-button|odds', re.I)},
    ]
    
    samples = []
    for pattern in betting_patterns:
        elements = soup.find_all(**pattern)[:5]  # First 5 matches
        for elem in elements:
            # Get the element and a couple levels of parent context
            sample = str(elem)
            if len(sample) < 500 and elem.parent:
                sample = str(elem.parent)
            samples.append(sample[:3000])  # Limit each sample
    
    if samples:
        result = "\n\n<!-- Sample elements -->\n".join(samples[:10])
        logger.info("Extracted %d HTML samples (%d chars)", len(samples[:10]), len(result))
        return result[:max_size]
    
    # Fallback: look for class patterns in raw HTML
    class_patterns = [
        r'class="[^"]*btmarket[^"]*"',
        r'class="[^"]*selection[^"]*"',
        r'class="[^"]*outcome[^"]*"',
        r'class="[^"]*betbutton[^"]*"',
        r'class="[^"]*odds[^"]*"',
    ]
    
    earliest_pos = len(html_content)
    for pattern in class_patterns:
        match = re.search(pattern, html_content, re.IGNORECASE)
        if match and match.start() < earliest_pos:
            earliest_pos = match.start()
    
    if earliest_pos < len(html_content):
        start_pos = max(0, earliest_pos - 500)
        return html_content[start_pos:start_pos + max_size]
    
    # Fallback: find body
    body_start = html_content.find('<body')
    if body_start > 0:
        return html_content[body_start:body_start + max_size]
    
    return html_content[:max_size]


def analyze_page_with_snapshot(
    snapshot: str,
    html_content: str,
    user_prompt: str,
    *,
    api_key: Optional[str] = None,
    model: str = DEFAULT_MODEL,
) -> ExtractionRule:
    """
    Use Claude to analyze a page snapshot and determine CSS selectors.
    
    Uses the lightweight accessibility snapshot for understanding content,
    plus a small HTML sample for CSS class identification.

    Args:
        snapshot: Playwright accessibility snapshot text.
        html_content: Raw HTML (for CSS class reference).
        user_prompt: What the user wants to extract.
        api_key: Anthropic API key.
        model: Claude model to use.

    Returns:
        ExtractionRule with CSS selectors.
    """
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("No API key. Set ANTHROPIC_API_KEY environment variable.")

    # Get a small HTML sample for CSS classes
    html_sample = _extract_html_sample(html_content, max_size=15000)

    prompt = ANALYSIS_PROMPT.format(
        user_prompt=user_prompt,
        snapshot=snapshot[:50000],  # Limit snapshot size too
        html_sample=html_sample,
    )

    logger.info("Sending snapshot analysis to Claude (%s) - %d chars", model, len(prompt))

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=model,
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )

    response_text = response.content[0].text.strip()
    logger.debug("Claude response: %s", response_text[:500])

    # Parse JSON response
    try:
        result = json.loads(response_text)
    except json.JSONDecodeError:
        json_match = re.search(r"\{.*\}", response_text, re.DOTALL)
        if json_match:
            result = json.loads(json_match.group(0))
        else:
            raise ValueError(f"Failed to parse Claude response as JSON: {response_text[:200]}")

    return ExtractionRule(
        market_name=result.get("market_name", "Unknown Market"),
        selection_selector=result["selection_selector"],
        name_selector=result["name_selector"],
        odds_selector=result["odds_selector"],
        description=result.get("description", ""),
    )


# Keep old function for backwards compatibility
def analyze_page_for_selectors(
    html_content: str,
    user_prompt: str,
    *,
    api_key: Optional[str] = None,
    model: str = DEFAULT_MODEL,
) -> ExtractionRule:
    """Legacy function - use analyze_page_with_snapshot instead."""
    html_sample = _extract_html_sample(html_content, max_size=50000)
    
    # Create a simple text representation as "snapshot"
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html_content, 'html.parser')
    text_content = soup.get_text(separator='\n', strip=True)[:20000]
    
    return analyze_page_with_snapshot(
        snapshot=text_content,
        html_content=html_content,
        user_prompt=user_prompt,
        api_key=api_key,
        model=model,
    )


def extract_with_rule(html_content: str, rule: ExtractionRule) -> ExtractedMarket:
    """
    Extract odds from HTML using a rule.
    Args:
        html_content: Raw HTML of the page.
        rule: ExtractionRule with CSS selectors.

    Returns:
        ExtractedMarket with selections.
    """
    soup = BeautifulSoup(html_content, "html.parser")
    selections = []

    # Find all selection containers
    selection_elements = soup.select(rule.selection_selector)
    logger.info("Found %d selection elements with selector '%s'", 
                len(selection_elements), rule.selection_selector)

    for elem in selection_elements:
        name = None
        odds = None
        
        # Extract name - check for data-name attribute first (common in betting sites)
        if elem.get('data-name'):
            name = elem.get('data-name')
        else:
            # Try the name selector
            name_elem = elem.select_one(rule.name_selector)
            if not name_elem:
                name_elem = elem.find(rule.name_selector.lstrip('.'))
            if name_elem:
                # Check for data-name on the found element too
                name = name_elem.get('data-name') or name_elem.get_text(strip=True)
        
        # Extract odds - check for data-odds attribute first
        if elem.get('data-odds'):
            odds = elem.get('data-odds')
        else:
            odds_elem = elem.select_one(rule.odds_selector)
            if not odds_elem:
                odds_elem = elem.find(rule.odds_selector.lstrip('.'))
            if odds_elem:
                odds = odds_elem.get('data-odds') or odds_elem.get_text(strip=True)

        if name and odds:
            selections.append(ExtractedSelection(name=name, odds=odds))

    return ExtractedMarket(market_name=rule.market_name, selections=selections)


def save_rule(domain: str, rule: ExtractionRule, rules_dir: str = "rules") -> str:
    """Save an extraction rule to disk."""
    os.makedirs(rules_dir, exist_ok=True)
    filepath = os.path.join(rules_dir, f"{domain}_selectors.json")
    
    rule_dict = {
        "market_name": rule.market_name,
        "selection_selector": rule.selection_selector,
        "name_selector": rule.name_selector,
        "odds_selector": rule.odds_selector,
        "description": rule.description,
    }
    
    with open(filepath, "w") as f:
        json.dump(rule_dict, f, indent=2)
    
    logger.info("Saved rule to %s", filepath)
    return filepath


def load_rule(domain: str, rules_dir: str = "rules") -> Optional[ExtractionRule]:
    """Load an extraction rule from disk."""
    filepath = os.path.join(rules_dir, f"{domain}_selectors.json")
    
    if not os.path.exists(filepath):
        return None
    
    with open(filepath) as f:
        rule_dict = json.load(f)
    
    return ExtractionRule(
        market_name=rule_dict["market_name"],
        selection_selector=rule_dict["selection_selector"],
        name_selector=rule_dict["name_selector"],
        odds_selector=rule_dict["odds_selector"],
        description=rule_dict.get("description", ""),
    )
