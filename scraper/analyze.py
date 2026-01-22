"""Use Claude to analyze a page and propose AutoScraper training examples."""

import json
import logging
import os
from typing import Dict, List, Optional

import anthropic
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 4096

ANALYSIS_PROMPT = """You are analyzing a sports betting webpage to extract specific odds data.

USER REQUEST: {user_prompt}

Below is the HTML content of the betting page. Your task is to:
1. Find the exact text values that match what the user is looking for
2. Return these as example strings that can be used to train a web scraper

Look for:
- Team/player names exactly as they appear on the page
- Odds values (decimal like "2.10", fractional like "5/2", or American like "+150")
- Any relevant labels or market names

Return a JSON object with:
{{
  "wanted_list": ["exact string 1", "exact string 2", ...],
  "explanation": "Brief explanation of what you found"
}}

The "wanted_list" should contain 3-10 EXACT strings as they appear in the HTML.
Include both team names AND their corresponding odds values.

HTML CONTENT:
{html_content}

Return ONLY the JSON object, no markdown:"""


def analyze_page_for_training(
    html_content: str,
    user_prompt: str,
    *,
    api_key: Optional[str] = None,
    model: str = DEFAULT_MODEL,
) -> Dict:
    """
    Use Claude to analyze a page and propose training examples.

    Args:
        html_content: Raw HTML of the betting page.
        user_prompt: What the user wants to extract (e.g., "3 way money line odds").
        api_key: Anthropic API key.
        model: Claude model to use.

    Returns:
        Dict with 'wanted_list' and 'explanation'.
    """
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("No API key. Set ANTHROPIC_API_KEY environment variable.")

    # Truncate HTML if too long (keep first 100k chars)
    max_html_len = 100000
    if len(html_content) > max_html_len:
        html_content = html_content[:max_html_len] + "\n... [truncated]"

    prompt = ANALYSIS_PROMPT.format(
        user_prompt=user_prompt,
        html_content=html_content,
    )

    logger.info("Sending page analysis request to Claude (%s)", model)

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )

    response_text = response.content[0].text.strip()
    logger.debug("Claude response: %s", response_text[:500])

    # Parse JSON response
    try:
        result = json.loads(response_text)
    except json.JSONDecodeError:
        # Try to extract JSON from response
        import re
        json_match = re.search(r"\{.*\}", response_text, re.DOTALL)
        if json_match:
            result = json.loads(json_match.group(0))
        else:
            raise ValueError(f"Could not parse Claude response as JSON: {response_text[:200]}")

    if "wanted_list" not in result:
        raise ValueError("Claude response missing 'wanted_list'")

    logger.info("Claude found %d training examples", len(result["wanted_list"]))
    logger.info("Explanation: %s", result.get("explanation", "N/A"))

    return result


def format_extracted_odds(
    extracted_data: Dict[str, List[str]],
    user_prompt: str,
    *,
    api_key: Optional[str] = None,
    model: str = DEFAULT_MODEL,
) -> Dict:
    """
    Use Claude to format raw extracted data into structured odds.

    Args:
        extracted_data: Raw data from AutoScraper.
        user_prompt: Original user request for context.
        api_key: Anthropic API key.
        model: Claude model.

    Returns:
        Structured odds dict.
    """
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("No API key. Set ANTHROPIC_API_KEY environment variable.")

    format_prompt = f"""Convert this raw extracted betting data into structured JSON.

USER REQUEST: {user_prompt}

RAW EXTRACTED DATA:
{json.dumps(extracted_data, indent=2)}

Return JSON matching this schema:
{{
  "event": {{"name": "Team A vs Team B", "start_time": null}},
  "markets": [
    {{
      "market_name": "3-Way Moneyline",
      "selections": [
        {{"name": "Team A", "price_decimal": 2.10}},
        {{"name": "Draw", "price_decimal": 3.50}},
        {{"name": "Team B", "price_decimal": 2.90}}
      ]
    }}
  ],
  "timestamp": "ISO timestamp"
}}

Convert any fractional or American odds to decimal. Return ONLY JSON:"""

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        messages=[{"role": "user", "content": format_prompt}],
    )

    response_text = response.content[0].text.strip()

    try:
        return json.loads(response_text)
    except json.JSONDecodeError:
        import re
        json_match = re.search(r"\{.*\}", response_text, re.DOTALL)
        if json_match:
            return json.loads(json_match.group(0))
        raise ValueError(f"Could not parse response: {response_text[:200]}")
