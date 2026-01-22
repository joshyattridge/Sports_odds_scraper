"""Text-based extraction using Playwright locators - works on ANY site."""

import json
import logging
import os
import re
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Tuple

import anthropic
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-haiku-4-5-20251001"


@dataclass
class TextRule:
    """A text-based rule for extracting odds."""
    market_name: str
    selections: List[Dict[str, str]]  # List of {"name": "Home", "odds": "11/8"} from initial scan
    odds_pattern: str  # Regex pattern for odds (e.g. r"\d+/\d+" or r"\d+\.\d+")
    layout: str  # "adjacent" (odds next to name) or "grouped" (name\nodds pattern)
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


# Prompt for Claude to analyze snapshot and return text patterns
ANALYSIS_PROMPT = """You are analyzing a sports betting webpage to identify odds.

USER REQUEST: {user_prompt}

Below is a TEXT SNAPSHOT of the page (what a user would see):

{snapshot}

---

Based on this snapshot, identify:
1. What market/bet type matches the user's request
2. The selection names (e.g., "Home", "Draw", "Away" or team names)
3. The odds next to each selection
4. The odds format (fractions like "11/8" or decimals like "2.50")

Return a JSON object with this EXACT structure:
{{
  "market_name": "Match Result" or similar description,
  "selections": [
    {{"name": "Home", "odds": "11/8"}},
    {{"name": "Draw", "odds": "5/2"}},
    {{"name": "Away", "odds": "19/10"}}
  ],
  "odds_pattern": "fraction" or "decimal",
  "layout": "adjacent",
  "description": "Brief explanation of what you found"
}}

IMPORTANT:
- Extract ALL selections you can find that match the user's request
- Use the EXACT text as shown in the snapshot for names
- Include the EXACT odds values as shown
- If there are multiple matches/events, include all of them
- For "odds_pattern": use "fraction" for odds like "11/8", "decimal" for "2.50", "american" for "+150"

Return ONLY valid JSON, no other text."""


def analyze_snapshot_for_text(
    snapshot: str,
    user_prompt: str,
    model: str = DEFAULT_MODEL,
) -> TextRule:
    """
    Send snapshot to Claude and get text-based extraction patterns.
    
    Args:
        snapshot: Text content of the page
        user_prompt: What the user wants to extract
        model: Claude model to use
        
    Returns:
        TextRule with selection names and patterns
    """
    client = anthropic.Anthropic()
    
    # Truncate snapshot if too long
    max_snapshot_len = 15000
    if len(snapshot) > max_snapshot_len:
        snapshot = snapshot[:max_snapshot_len] + "\n... [truncated]"
    
    prompt = ANALYSIS_PROMPT.format(
        user_prompt=user_prompt,
        snapshot=snapshot,
    )
    
    logger.info("Sending snapshot to Claude (%s) - %d chars", model, len(prompt))
    
    response = client.messages.create(
        model=model,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    
    result_text = response.content[0].text.strip()
    logger.debug("Claude response: %s", result_text[:500])
    
    # Parse JSON from response
    try:
        # Try to extract JSON if wrapped in markdown
        if "```json" in result_text:
            json_match = re.search(r"```json\s*(.*?)\s*```", result_text, re.DOTALL)
            if json_match:
                result_text = json_match.group(1)
        elif "```" in result_text:
            json_match = re.search(r"```\s*(.*?)\s*```", result_text, re.DOTALL)
            if json_match:
                result_text = json_match.group(1)
        
        data = json.loads(result_text)
    except json.JSONDecodeError as e:
        logger.error("Failed to parse Claude response as JSON: %s", e)
        logger.error("Response was: %s", result_text[:1000])
        raise ValueError(f"Claude returned invalid JSON: {e}")
    
    # Convert odds_pattern to regex
    pattern_map = {
        "fraction": r"\d+/\d+",
        "decimal": r"\d+\.\d+",
        "american": r"[+-]\d+",
    }
    odds_pattern = pattern_map.get(data.get("odds_pattern", "fraction"), r"\d+/\d+")
    
    return TextRule(
        market_name=data.get("market_name", "Unknown Market"),
        selections=data.get("selections", []),
        odds_pattern=odds_pattern,
        layout=data.get("layout", "adjacent"),
        description=data.get("description", ""),
    )


def extract_with_text_rule(
    snapshot: str,
    rule: TextRule,
) -> ExtractedMarket:
    """
    Extract current odds from snapshot using text patterns.
    
    This is the FAST method - no LLM needed, just text matching.
    Handles both flat selections and nested event/odds structures.
    
    Args:
        snapshot: Current page text content
        rule: TextRule with known selection names or events
        
    Returns:
        ExtractedMarket with current odds
    """
    selections = []
    odds_regex = re.compile(rule.odds_pattern)
    
    # Check if we have nested event structure (from Claude's rich output)
    if rule.selections and isinstance(rule.selections[0], dict):
        first = rule.selections[0]
        
        # Check for nested "odds" or "picks" array (event structure)
        # Claude sometimes uses "odds", sometimes "picks"
        # Event key can be "event" or "match"
        has_event = "event" in first or "match" in first
        has_odds = "odds" in first or "picks" in first
        
        if has_event and has_odds:
            # Rich event-based structure
            return _extract_events_from_snapshot(snapshot, rule)
        
        # Check for simple name/odds pairs
        if "name" in first and "odds" in first:
            return _extract_simple_from_snapshot(snapshot, rule)
    
    # Fallback: try to find any odds patterns in the snapshot
    return _extract_any_odds_from_snapshot(snapshot, rule)


def _extract_events_from_snapshot(snapshot: str, rule: TextRule) -> ExtractedMarket:
    """Extract odds for events with nested structure."""
    selections = []
    odds_regex = re.compile(rule.odds_pattern)
    lines = snapshot.split('\n')
    
    for event_data in rule.selections:
        # Handle both "event" and "match" keys (Claude varies)
        event_name = event_data.get("event") or event_data.get("match") or ""
        # Handle both "odds" and "picks" keys (Claude varies)
        event_odds = event_data.get("odds") or event_data.get("picks") or []
        
        if not event_name:
            continue
        
        # Find the event in the snapshot
        event_found = False
        event_line_idx = -1
        
        # Try to match event name (teams might be formatted differently)
        # e.g. "Fenerbahce SK vs Aston Villa" or "Fenerbahce v Aston Villa"
        event_parts = re.split(r'\s+vs?\s+', event_name, flags=re.IGNORECASE)
        
        for i, line in enumerate(lines):
            line_lower = line.lower()
            # Check if both team parts appear
            if len(event_parts) >= 2:
                if (event_parts[0].lower()[:10] in line_lower or 
                    event_parts[1].lower()[:10] in line_lower):
                    event_found = True
                    event_line_idx = i
                    break
            elif event_name.lower()[:15] in line_lower:
                event_found = True
                event_line_idx = i
                break
        
        if not event_found:
            continue
        
        # Look for odds near this event (within next 10 lines)
        search_text = '\n'.join(lines[event_line_idx:event_line_idx + 15])
        found_odds = odds_regex.findall(search_text)
        
        # Map found odds to selection names (HOME, DRAW, AWAY)
        for j, odds_info in enumerate(event_odds):
            selection_name = odds_info.get("name", f"Selection {j+1}")
            
            if j < len(found_odds):
                # Use the odds we found in the snapshot
                selections.append(ExtractedSelection(
                    name=f"{event_name} - {selection_name}",
                    odds=found_odds[j]
                ))
            else:
                # Use the stored odds (from setup)
                selections.append(ExtractedSelection(
                    name=f"{event_name} - {selection_name}",
                    odds=odds_info.get("odds", "N/A")
                ))
    
    return ExtractedMarket(
        market_name=rule.market_name,
        selections=selections,
    )


def _extract_simple_from_snapshot(snapshot: str, rule: TextRule) -> ExtractedMarket:
    """Extract odds using simple name/odds pairs."""
    selections = []
    odds_regex = re.compile(rule.odds_pattern)
    lines = snapshot.split('\n')
    full_text = snapshot
    
    for known in rule.selections:
        name = known.get("name", "")
        if not name:
            continue
            
        # Find the name in the snapshot
        pattern = re.compile(
            re.escape(name) + r"[:\s]*(" + rule.odds_pattern + r")",
            re.IGNORECASE
        )
        
        match = pattern.search(full_text)
        if match:
            selections.append(ExtractedSelection(name=name, odds=match.group(1)))
            continue
        
        # Try finding name and odds on same or adjacent lines
        for i, line in enumerate(lines):
            if name.lower() in line.lower():
                odds_match = odds_regex.search(line)
                if odds_match:
                    selections.append(ExtractedSelection(name=name, odds=odds_match.group()))
                    break
                if i + 1 < len(lines):
                    odds_match = odds_regex.search(lines[i + 1])
                    if odds_match:
                        selections.append(ExtractedSelection(name=name, odds=odds_match.group()))
                        break
    
    return ExtractedMarket(
        market_name=rule.market_name,
        selections=selections,
    )


# Junk patterns to filter out - these aren't real betting selections
JUNK_PATTERNS = [
    r'betslip',
    r'lucky\s*dip',
    r'add\s+(to|all)',
    r'\bmins?\b',
    r'\bsecs?\b',
    r'license',
    r'licence',
    r'auspices',
    r'mga/',
    r'crp/',
    r'registered',
    r'limited',
    r'copyright',
    r'terms',
    r'privacy',
    r'cookie',
    r'responsible\s*gaming',
    r'gamble\s*aware',
    r'deposit',
    r'withdraw',
    r'bonus',
    r'promo',
    r'offer\s*period',
    r'free\s*spins',
    r'min\s*odds',
    r'counter\s*party',
    r'\bwin(ner)?\s*\d{4}',  # "Winner 2025/26"
    r'each\s*way',
    r'enhanced\s*place',
    r'apply|applies',
    r'customers?:',
    r'joe\s*cole',  # Specific promo name
    r'for\s*example',
    r'our\s+football',
    r'displayed\s+as',
    r'\bwas\b:?$',  # "WAS" (old price)
    r'potential\s+(returns|profit)',
    r'fourfold',
    r'acca\b',
    r'betting\s+offer',
    r'how\s+much',
    r'want\s+to\s+know',
    r'come\s+from',
    r'range\s+of',
    r'tremendous',
    r'popular',
]

JUNK_REGEX = re.compile('|'.join(JUNK_PATTERNS), re.IGNORECASE)

# Pattern for time-like strings (HH:MM format)
TIME_PATTERN = re.compile(r'^\d{1,2}:\d{2}$')


def _is_valid_odds(odds_str: str) -> bool:
    """Check if odds string looks like valid betting odds, not a date or junk."""
    # Parse the fraction
    if '/' not in odds_str:
        return False
    
    parts = odds_str.split('/')
    if len(parts) != 2:
        return False
    
    try:
        numerator = int(parts[0])
        denominator = int(parts[1])
    except ValueError:
        return False
    
    # Filter out date-like patterns (day/month or month/year)
    # Dates typically have: 01-31 / 01-12 or 01-12 / 00-99
    if 1 <= numerator <= 31 and 1 <= denominator <= 12:
        # Could be a date like 22/09 - be suspicious if denominator is month-like
        # But 22/9 could also be odds, so check if it's a common date pattern
        if denominator <= 12 and numerator >= 1:
            # If numerator > 12 and denominator <= 12, likely a date
            if numerator > 12:
                return False
    
    # Filter out year patterns like 2025/26, 2024/25
    if numerator >= 2000 and denominator <= 99:
        return False
    
    # Valid odds typically have reasonable ranges
    # Very unusual: 121/2006 (license number), 131/2006
    if denominator > 100 and numerator > 100:
        return False
    
    # Odds like 0/1 or 1/0 are invalid
    if numerator == 0 or denominator == 0:
        return False
    
    return True


def _is_junk_line(line: str) -> bool:
    """Check if a line contains junk/promotional text."""
    return bool(JUNK_REGEX.search(line))


def _is_valid_name(name: str) -> bool:
    """Check if a name looks like a valid selection/team name."""
    name = name.strip()
    
    # Too short
    if len(name) < 2:
        return False
    
    # Just a number or time
    if name.isdigit():
        return False
    
    # Time pattern like "17:45" or "20:00"
    if TIME_PATTERN.match(name):
        return False
    
    # Single letter/number like "1", "X", "2" - these ARE valid (home/draw/away)
    if name in ('1', 'X', '2', 'x'):
        return True
    
    # Very short generic labels
    if name.upper() in ('WAS', 'EVS', 'SP'):
        return False
    
    # Contains junk
    if _is_junk_line(name):
        return False
    
    return True


def _extract_any_odds_from_snapshot(snapshot: str, rule: TextRule) -> ExtractedMarket:
    """Fallback: extract all odds patterns found in snapshot."""
    selections = []
    odds_regex = re.compile(rule.odds_pattern)
    
    # Find all odds in the snapshot with surrounding context
    lines = snapshot.split('\n')
    
    for i, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue
        
        # Skip junk lines
        if _is_junk_line(line):
            continue
            
        odds_matches = list(odds_regex.finditer(line))
        
        for match in odds_matches:
            odds = match.group()
            
            # Validate odds
            if not _is_valid_odds(odds):
                continue
            
            # Get text before the odds as context
            name_part = line[:match.start()].strip()
            name_part = re.sub(r'[:\-\s]+$', '', name_part)
            
            # Skip if name part is junk or invalid
            if name_part and _is_junk_line(name_part):
                continue
            
            # Validate name
            if name_part and _is_valid_name(name_part):
                selections.append(ExtractedSelection(name=name_part, odds=odds))
            elif i > 0:
                prev_line = lines[i-1].strip()
                if prev_line and not odds_regex.search(prev_line) and _is_valid_name(prev_line):
                    selections.append(ExtractedSelection(name=prev_line, odds=odds))
    
    return ExtractedMarket(
        market_name=rule.market_name,
        selections=selections,
    )


def extract_all_odds_from_snapshot(
    snapshot: str,
    odds_pattern: str = r"\d+/\d+",
) -> List[Tuple[str, str]]:
    """
    Extract ALL name-odds pairs from snapshot using pattern matching.
    
    This finds patterns like:
    - "Home 11/8" 
    - "Arsenal\n2.50"
    - "Over 2.5 Goals 4/5"
    
    Returns list of (name, odds) tuples.
    """
    results = []
    odds_regex = re.compile(odds_pattern)
    
    lines = snapshot.split('\n')
    
    for i, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue
            
        # Check if this line has odds
        odds_matches = list(odds_regex.finditer(line))
        
        for match in odds_matches:
            odds = match.group()
            # Get text before the odds as the name
            name_part = line[:match.start()].strip()
            
            # Clean up name (remove trailing punctuation)
            name_part = re.sub(r'[:\-\s]+$', '', name_part)
            
            if name_part and len(name_part) > 1:
                results.append((name_part, odds))
            elif i > 0:
                # Maybe name is on previous line
                prev_line = lines[i-1].strip()
                if prev_line and not odds_regex.search(prev_line):
                    results.append((prev_line, odds))
    
    return results


def save_text_rule(domain: str, rule: TextRule, rules_dir: str = "rules") -> str:
    """Save a text-based rule to JSON file."""
    os.makedirs(rules_dir, exist_ok=True)
    
    filename = f"{domain}_text_rule.json"
    filepath = os.path.join(rules_dir, filename)
    
    data = {
        "market_name": rule.market_name,
        "selections": rule.selections,
        "odds_pattern": rule.odds_pattern,
        "layout": rule.layout,
        "description": rule.description,
    }
    
    with open(filepath, "w") as f:
        json.dump(data, f, indent=2)
    
    logger.info("Saved text rule to %s", filepath)
    return filepath


def load_text_rule(domain: str, rules_dir: str = "rules") -> Optional[TextRule]:
    """Load a text-based rule from JSON file."""
    filename = f"{domain}_text_rule.json"
    filepath = os.path.join(rules_dir, filename)
    
    if not os.path.exists(filepath):
        return None
    
    with open(filepath, "r") as f:
        data = json.load(f)
    
    return TextRule(
        market_name=data["market_name"],
        selections=data["selections"],
        odds_pattern=data["odds_pattern"],
        layout=data.get("layout", "adjacent"),
        description=data.get("description", ""),
    )
