"""Normalisation of extracted data using Claude API."""

import json
import logging
import os
from typing import Any, Dict, List, Optional

import anthropic
from dotenv import load_dotenv
from pydantic import ValidationError

# Load environment variables from .env file
load_dotenv()

from .models import RawExtractionResult, ScrapedOdds

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 2048

NORMALISATION_PROMPT = """You are a data normalisation assistant. Your task is to convert raw extracted betting odds data into a structured JSON format.

Given the extracted text values from a sports betting page, produce a JSON object matching this exact schema:

{
  "event": {
    "name": "string (e.g., 'Team A vs Team B')",
    "start_time": "string or null (ISO format if available)"
  },
  "markets": [
    {
      "market_name": "string (e.g., 'Match Result', '1X2', 'Moneyline')",
      "selections": [
        {
          "name": "string (team name or outcome like 'Draw')",
          "price_decimal": number (decimal odds, must be > 1.0)
        }
      ]
    }
  ]
}

Rules:
1. Output ONLY valid JSON, no markdown, no explanation.
2. Infer team names and match them with their corresponding odds.
3. Convert fractional or American odds to decimal if needed.
4. Group selections into logical markets (e.g., Match Result, Over/Under).
5. If data is ambiguous, make reasonable inferences based on typical betting formats.
6. Ensure price_decimal values are floats greater than 1.0.
7. Use null for start_time if not clearly present in the data.

Extracted data:
{extracted_data}

Context (URL): {url}

Output the structured JSON:"""


def normalise_odds(
    raw_result: RawExtractionResult,
    *,
    api_key: Optional[str] = None,
    model: str = DEFAULT_MODEL,
    validate: bool = True,
) -> ScrapedOdds:
    """
    Normalise raw extracted data into structured odds using Claude.

    Args:
        raw_result: Raw extraction result from scraping.
        api_key: Anthropic API key (defaults to ANTHROPIC_API_KEY env var).
        model: Claude model to use.
        validate: Whether to validate output against Pydantic model.

    Returns:
        Validated ScrapedOdds object.

    Raises:
        ValueError: If normalisation fails or output is invalid.
        anthropic.APIError: On API errors.
    """
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError(
            "No API key provided. Set ANTHROPIC_API_KEY environment variable "
            "or pass api_key parameter."
        )

    # Format extracted data for the prompt
    formatted_data = _format_extracted_data(raw_result.extracted_data)

    prompt = NORMALISATION_PROMPT.format(
        extracted_data=formatted_data,
        url=raw_result.url,
    )

    logger.info("Sending normalisation request to Claude (%s)", model)
    logger.debug("Prompt length: %d chars", len(prompt))

    # Call Claude API
    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )

    # Extract response text
    response_text = response.content[0].text.strip()
    logger.debug("Claude response: %s", response_text[:500])

    # Parse JSON response
    try:
        parsed = json.loads(response_text)
    except json.JSONDecodeError as e:
        # Try to extract JSON from response if wrapped in markdown
        parsed = _extract_json_from_response(response_text)
        if parsed is None:
            raise ValueError(f"Failed to parse Claude response as JSON: {e}") from e

    # Validate against schema
    if validate:
        try:
            return ScrapedOdds.model_validate(parsed)
        except ValidationError as e:
            raise ValueError(f"Claude output failed validation: {e}") from e

    return ScrapedOdds.model_validate(parsed)


def _format_extracted_data(data: Dict[str, List[str]]) -> str:
    """Format extracted data for inclusion in prompt."""
    lines = []
    for key, values in data.items():
        if values:
            lines.append(f"[{key}]:")
            for v in values[:50]:  # Limit to prevent token overflow
                lines.append(f"  - {v}")
    return "\n".join(lines) if lines else "(no data extracted)"


def _extract_json_from_response(text: str) -> Optional[Dict[str, Any]]:
    """Attempt to extract JSON from a response that may have markdown wrapping."""
    # Try to find JSON block
    import re

    # Look for ```json ... ``` blocks
    json_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group(1))
        except json.JSONDecodeError:
            pass

    # Try to find raw JSON object
    brace_match = re.search(r"\{.*\}", text, re.DOTALL)
    if brace_match:
        try:
            return json.loads(brace_match.group(0))
        except json.JSONDecodeError:
            pass

    return None


def normalise_with_retry(
    raw_result: RawExtractionResult,
    *,
    api_key: Optional[str] = None,
    model: str = DEFAULT_MODEL,
    max_retries: int = 2,
) -> ScrapedOdds:
    """
    Normalise with automatic retry on validation failures.

    Args:
        raw_result: Raw extraction result from scraping.
        api_key: Anthropic API key.
        model: Claude model to use.
        max_retries: Maximum retry attempts.

    Returns:
        Validated ScrapedOdds object.
    """
    last_error = None

    for attempt in range(max_retries + 1):
        try:
            return normalise_odds(
                raw_result,
                api_key=api_key,
                model=model,
                validate=True,
            )
        except ValueError as e:
            last_error = e
            logger.warning("Normalisation attempt %d failed: %s", attempt + 1, e)

    raise ValueError(f"Normalisation failed after {max_retries + 1} attempts: {last_error}")
