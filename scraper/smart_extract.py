"""Smart text-based extraction - learns patterns from the page itself."""

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import anthropic
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-haiku-4-5-20251001"


# Predefined market types with their expected structure
MARKET_TYPES = {
    "h2h_3way": {
        "name": "Match Result (1X2)",
        "selections": 3,
        "labels": ["Home", "Draw", "Away"],
        "prompt": "Find the main match result / 3-way / 1X2 odds (Home, Draw, Away)",
    },
    "h2h_2way": {
        "name": "Match Winner (Moneyline)",
        "selections": 2,
        "labels": ["Home", "Away"],
        "prompt": "Find the match winner / moneyline / head-to-head odds (2-way, no draw)",
    },
    "btts": {
        "name": "Both Teams To Score",
        "selections": 2,
        "labels": ["Yes", "No"],
        "prompt": "Find the both teams to score (BTTS) odds - Yes and No",
    },
}


def get_market_config(market: str) -> dict:
    """Get market config, supporting dynamic over_under lines like over_under_2.5."""
    # Check for over_under with line (e.g., over_under_2.5)
    if market.startswith("over_under_"):
        line = market.replace("over_under_", "")
        return {
            "name": f"Over/Under {line} Goals",
            "selections": 2,
            "labels": [f"Over {line}", f"Under {line}"],
            "prompt": f"Find the Over/Under {line} goals odds. Look for '{line}' in the over/under section and return ONLY the two odds on the lines immediately after it. Set start_marker to '{line}' (the goal line) and end_marker to the next line number like '3.5' or '4.5' that appears after.",
        }
    
    # Standard market types
    return MARKET_TYPES.get(market)


@dataclass
class SmartRule:
    """A learned rule for extracting odds."""
    market_name: str
    odds_pattern: str  # Regex for odds format (e.g. r"\d+/\d+")
    name_position: str  # "before_same_line", "before_prev_line", "after_same_line", "grouped_before"
    name_separator: str  # What separates name from odds (e.g. " ", "\n", ":")
    sample_odds: List[str]  # Example odds found during setup
    description: str
    group_size: int = 3  # For grouped odds (1/X/2)
    market_type: str = ""  # e.g. "h2h_3way", "over_under"
    selection_labels: List[str] = field(default_factory=list)  # ["Home", "Draw", "Away"]
    start_marker: str = ""  # Text that appears before the odds (e.g. "90 Minutes")
    end_marker: str = ""  # Text that appears after the odds (e.g. "Popular Bet Builders")


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


# Prompt for Claude - just ask for the odds values
ANALYSIS_PROMPT = """You are analyzing a sports betting webpage snapshot.

USER REQUEST: {user_prompt}

TEXT SNAPSHOT OF THE PAGE:
{snapshot}

---

Your task: Find the betting odds that match the user's request AND identify the context markers.

Return a JSON object with ONLY this structure:
{{
  "market_name": "Match Result" or similar description,
  "odds_format": "fraction" or "decimal" or "american",
  "odds_found": ["11/8", "5/2", "2/1"],
  "start_marker": "The exact text/heading that appears BEFORE these odds (e.g. '90 Minutes', 'Match Result')",
  "end_marker": "The exact text/heading that appears AFTER these odds (e.g. 'Popular Bet Builders', 'Both Teams To Score')",
  "description": "Brief note about what you found"
}}

IMPORTANT:
- List ONLY the odds values themselves (like "11/8", "2.50", "+150")
- For start_marker: find the section header or label that appears just before the odds
- For end_marker: find the next section header that appears after the odds
- These markers help locate the odds reliably on future page loads

Return ONLY valid JSON."""


def analyze_snapshot_for_odds(
    snapshot: str,
    user_prompt: str,
    model: str = DEFAULT_MODEL,
) -> Tuple[str, str, List[str], str, str]:
    """
    Ask Claude to identify odds values and context markers.
    
    Returns: (market_name, odds_pattern, list_of_odds, start_marker, end_marker)
    """
    client = anthropic.Anthropic()
    
    # Truncate snapshot if too long
    max_len = 15000
    if len(snapshot) > max_len:
        snapshot = snapshot[:max_len] + "\n... [truncated]"
    
    prompt = ANALYSIS_PROMPT.format(
        user_prompt=user_prompt,
        snapshot=snapshot,
    )
    
    logger.info("Asking Claude to identify odds values - %d chars", len(prompt))
    
    response = client.messages.create(
        model=model,
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    
    result_text = response.content[0].text.strip()
    
    # Parse JSON
    try:
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
        logger.error("Failed to parse JSON: %s", e)
        raise ValueError(f"Claude returned invalid JSON: {e}")
    
    market_name = data.get("market_name", "Unknown")
    odds_format = data.get("odds_format", "fraction")
    odds_found = data.get("odds_found", [])
    start_marker = data.get("start_marker", "")
    end_marker = data.get("end_marker", "")
    
    # Convert format to regex
    pattern_map = {
        "fraction": r"\d+/\d+",
        "decimal": r"\d+\.\d+",
        "american": r"[+-]\d+",
    }
    odds_pattern = pattern_map.get(odds_format, r"\d+/\d+")
    
    return market_name, odds_pattern, odds_found, start_marker, end_marker


def learn_pattern_from_snapshot(
    snapshot: str,
    odds_list: List[str],
    odds_pattern: str,
) -> Tuple[str, str, dict]:
    """
    Learn how names relate to odds by analyzing the snapshot structure.
    
    Returns: (name_position, name_separator, extra_info)
    """
    lines = snapshot.split('\n')
    odds_regex = re.compile(odds_pattern)
    
    # Track what we find
    patterns_found = {
        "before_same_line": 0,
        "before_prev_line": 0,
        "after_same_line": 0,
        "grouped_odds": 0,  # Odds appear in groups (like 1/X/2)
    }
    
    # First, find where odds appear in the snapshot
    odds_positions = []
    for i, line in enumerate(lines):
        line_stripped = line.strip()
        if odds_regex.fullmatch(line_stripped):
            odds_positions.append(i)
    
    # Check if odds come in groups (within 4 lines of each other)
    # This handles cases like: Team1/Team2/Time/Odds1/Odds2/Odds3 (gaps of 1)
    # Or: 1/space/Odds1/X/space/Odds2/2/space/Odds3 (gaps of 3)
    if len(odds_positions) >= 3:
        close_groups = 0
        for i in range(len(odds_positions) - 2):
            # Check if 3 odds appear within 8 lines total
            if odds_positions[i+2] - odds_positions[i] <= 8:
                close_groups += 1
        
        if close_groups >= 2:
            patterns_found["grouped_odds"] = close_groups
            logger.info("Detected grouped odds pattern (e.g., 1/X/2 match odds)")
    
    # Also check for name patterns
    for odds_value in odds_list[:10]:
        for i, line in enumerate(lines):
            if odds_value in line:
                idx = line.find(odds_value)
                before_text = line[:idx].strip()
                after_text = line[idx + len(odds_value):].strip()
                
                if before_text and len(before_text) > 1 and not odds_regex.match(before_text):
                    patterns_found["before_same_line"] += 1
                elif after_text and len(after_text) > 1 and not odds_regex.match(after_text):
                    patterns_found["after_same_line"] += 1
                elif i > 0:
                    prev_line = lines[i-1].strip()
                    if prev_line and not odds_regex.search(prev_line):
                        patterns_found["before_prev_line"] += 1
                break
    
    # Determine pattern
    extra_info = {}
    
    if patterns_found["grouped_odds"] >= 2:
        # Grouped odds - names are further back
        best_pattern = "grouped_before"
        separator = "\n"
        extra_info["group_size"] = 3  # Typically 1/X/2
    else:
        best_pattern = max(patterns_found, key=patterns_found.get)
        separator = "\n" if best_pattern == "before_prev_line" else " "
    
    logger.info("Learned pattern: %s (separator: %r, extra: %s)", best_pattern, separator, extra_info)
    return best_pattern, separator, extra_info


def extract_with_smart_rule(
    snapshot: str,
    rule: SmartRule,
) -> ExtractedMarket:
    """
    Extract odds using the learned pattern.
    Uses context markers (start_marker/end_marker) if available for precise extraction.
    """
    odds_regex = re.compile(rule.odds_pattern)
    
    # If we have context markers, extract only from that section
    if rule.start_marker:
        snapshot = _extract_section(snapshot, rule.start_marker, rule.end_marker)
    
    lines = snapshot.split('\n')
    
    # Find all odds in the section
    odds_found = []
    for line in lines:
        line_stripped = line.strip()
        if odds_regex.fullmatch(line_stripped) and _is_valid_odds(line_stripped):
            odds_found.append(line_stripped)
    
    # Use selection_labels from rule if available, or get from market config
    market_config = get_market_config(rule.market_type) if rule.market_type else None
    
    if market_config or rule.selection_labels:
        expected_count = len(rule.selection_labels) if rule.selection_labels else market_config["selections"]
        labels = rule.selection_labels or market_config["labels"]
        
        # Take only the expected number of odds
        odds_found = odds_found[:expected_count]
        
        # Create selections with proper labels
        selections = []
        for i, odds in enumerate(odds_found):
            label = labels[i] if i < len(labels) else f"Selection {i+1}"
            selections.append(ExtractedSelection(name=label, odds=odds))
        
        return ExtractedMarket(
            market_name=rule.market_name,
            selections=selections,
        )
    
    # Fallback: use old grouped extraction
    if rule.name_position == "grouped_before":
        return _extract_grouped_odds(lines, rule, odds_regex)
    else:
        return _extract_line_based_odds(lines, rule, odds_regex)


def _extract_section(snapshot: str, start_marker: str, end_marker: str) -> str:
    """
    Extract text between start_marker and end_marker.
    Returns the section of text, or full snapshot if markers not found.
    """
    lines = snapshot.split('\n')
    start_idx = 0
    end_idx = len(lines)
    
    # Find start marker (case-insensitive)
    for i, line in enumerate(lines):
        if start_marker.lower() in line.lower():
            start_idx = i + 1  # Start after the marker line
            break
    
    # Find end marker (case-insensitive)
    if end_marker:
        for i, line in enumerate(lines[start_idx:], start=start_idx):
            if end_marker.lower() in line.lower():
                end_idx = i
                break
    
    return '\n'.join(lines[start_idx:end_idx])


def _extract_line_based_odds(
    lines: List[str],
    rule: SmartRule,
    odds_regex: re.Pattern,
) -> ExtractedMarket:
    """Extract odds using line-by-line pattern matching."""
    selections = []
    
    # Labels that indicate selection type (not the actual name)
    generic_labels = {'home', 'draw', 'away', '1', 'x', '2'}
    
    # Junk patterns to skip
    skip_patterns = ['see all', 'bet builder', 'form:', 'seo ', 't:', 'all markets', 'player to']
    
    # Time pattern
    time_pattern = re.compile(r'^\d{1,2}:\d{2}$')
    
    # Track current event context
    current_event = None
    last_event_line = -100  # Track when we last saw an event
    
    for i, line in enumerate(lines):
        line_stripped = line.strip()
        if not line_stripped:
            continue
        
        lower_line = line_stripped.lower()
        
        # Skip junk lines
        if any(skip in lower_line for skip in skip_patterns):
            continue
        
        # Check if this line has odds
        match = odds_regex.search(line_stripped)
        if not match:
            # This might be event context - store it if it looks like team/event name
            if (lower_line not in generic_labels and 
                len(line_stripped) > 2 and 
                not line_stripped.upper().startswith('BET ') and
                'TODAY' not in line_stripped.upper() and
                ':' not in line_stripped[:5] and  # Not a time like "17:45"
                not any(c.isdigit() for c in line_stripped[:3])):  # Not starting with numbers
                # Could be a team or event name
                current_event = line_stripped
                last_event_line = i
            continue
        
        # If we're too far from the last event, clear context
        if i - last_event_line > 10:
            current_event = None
        
        # Found odds - extract name
        odds_value = match.group()
        name = ""
        
        if rule.name_position == "before_same_line":
            before_text = line_stripped[:match.start()].strip()
            before_text = re.sub(r'[:\-\s]+$', '', before_text)
            if before_text and not odds_regex.match(before_text):
                name = before_text
                
        elif rule.name_position == "after_same_line":
            after_text = line_stripped[match.end():].strip()
            after_text = re.sub(r'^[:\-\s]+', '', after_text)
            if after_text and not odds_regex.match(after_text):
                name = after_text
                
        elif rule.name_position == "before_prev_line":
            if i > 0:
                prev_line = lines[i-1].strip()
                if prev_line and not odds_regex.search(prev_line):
                    name = prev_line
        
        if name:
            # Skip if name contains junk
            if any(skip in name.lower() for skip in skip_patterns):
                continue
            
            # Skip time patterns like "17:45"
            if time_pattern.match(name):
                continue
                
            # Check if name is a generic label
            if name.lower() in generic_labels:
                # Use event context if available
                if current_event:
                    full_name = f"{current_event} - {name.upper()}"
                else:
                    full_name = name.upper()
            else:
                full_name = name
            
            selections.append(ExtractedSelection(name=full_name, odds=odds_value))
    
    return ExtractedMarket(
        market_name=rule.market_name,
        selections=selections,
    )


def _is_valid_odds(odds_str: str) -> bool:
    """
    Check if a string looks like valid betting odds.
    Filters out single digits, counters, etc.
    """
    odds_str = odds_str.strip()
    
    # Fractional odds must have a slash
    if '/' in odds_str:
        parts = odds_str.split('/')
        if len(parts) == 2:
            try:
                num, denom = int(parts[0]), int(parts[1])
                # Valid fractional odds have reasonable values
                return num > 0 and denom > 0 and num <= 1000 and denom <= 100
            except ValueError:
                return False
    
    # Decimal odds (e.g., "1.50", "2.00")
    if '.' in odds_str:
        try:
            val = float(odds_str)
            return 1.0 <= val <= 1000.0
        except ValueError:
            return False
    
    # Single digit without slash is NOT valid odds (e.g., "2", "3")
    # These are often counters or labels
    if odds_str.isdigit():
        return False
    
    return False


def _extract_grouped_odds(
    lines: List[str],
    rule: SmartRule,
    odds_regex: re.Pattern,
) -> ExtractedMarket:
    """
    Extract odds that appear in groups (like match result 1/X/2).
    Handles both:
    - Unlabeled groups: odds appear consecutively, labels inferred (HOME/DRAW/AWAY)
    - Labeled groups: each odd has a label on previous line (HOME, DRAW, AWAY)
    """
    selections = []
    default_labels = ['HOME', 'DRAW', 'AWAY']
    # Only text labels count - single digits can be junk
    text_label_set = {'home', 'draw', 'away', 'x'}
    
    # Collect ALL valid odds and check for labels
    all_odds = []  # [(line_num, odds, has_label)]
    for i, line in enumerate(lines):
        line_stripped = line.strip()
        if odds_regex.fullmatch(line_stripped) and _is_valid_odds(line_stripped) and i > 0:
            prev = lines[i-1].strip().lower()
            has_label = prev in text_label_set
            all_odds.append((i, line_stripped, has_label))
    
    # Count labeled vs unlabeled
    labeled_count = sum(1 for _, _, has_label in all_odds if has_label)
    unlabeled_count = len(all_odds) - labeled_count
    
    # If more than 40% of odds have labels, use labeled extraction
    # This handles cases where specials at the top don't have labels but main odds do
    has_labels = len(all_odds) > 0 and (labeled_count / len(all_odds)) > 0.4
    
    if has_labels:
        return _extract_labeled_odds(lines, rule, odds_regex, default_labels)
    else:
        return _extract_unlabeled_grouped_odds(lines, rule, odds_regex, default_labels)


def _extract_labeled_odds(
    lines: List[str],
    rule: SmartRule,
    odds_regex: re.Pattern,
    selection_labels: List[str],
) -> ExtractedMarket:
    """
    Extract odds where each odd has a label on the previous line.
    Structure: TeamA / TeamB / HOME / 11/8 / DRAW / 5/2 / AWAY / 2/1
    """
    selections = []
    label_set = {'home', 'draw', 'away', '1', 'x', '2'}
    time_pattern = re.compile(r'^\d{1,2}:\d{2}')
    
    # Find all labeled odds (only valid ones)
    labeled_odds = []  # [(line_num, label, odds)]
    for i, line in enumerate(lines):
        line_stripped = line.strip()
        if odds_regex.fullmatch(line_stripped) and _is_valid_odds(line_stripped) and i > 0:
            prev = lines[i-1].strip()
            if prev.lower() in label_set:
                labeled_odds.append((i, prev.upper(), line_stripped))
    
    # Group by events (consecutive labeled odds)
    groups = []
    current_group = []
    
    for idx, (line_num, label, odds) in enumerate(labeled_odds):
        if not current_group:
            current_group = [(line_num, label, odds)]
        elif line_num - current_group[-1][0] <= 5:  # Within 5 lines
            current_group.append((line_num, label, odds))
        else:
            if len(current_group) >= 2:
                groups.append(current_group)
            current_group = [(line_num, label, odds)]
    
    if len(current_group) >= 2:
        groups.append(current_group)
    
    # For each group, find teams
    date_pattern = re.compile(r'^\d{1,2}\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)', re.IGNORECASE)
    
    for group in groups:
        first_odds_line = group[0][0]
        
        # Look back to find team names (skip labels, times, etc.)
        teams = []
        for j in range(first_odds_line - 2, max(0, first_odds_line - 15), -1):
            line = lines[j].strip()
            if not line:
                continue
            
            lower = line.lower()
            
            # Skip labels, time, date, etc.
            if (lower in label_set or
                time_pattern.match(line) or
                date_pattern.match(line) or
                lower in {'today', 'tomorrow', 'bet builder', 'upcoming matches'} or
                'today' in lower):
                continue
            
            # Skip junk
            if any(skip in lower for skip in ['times backed', 'add to', 'boost']):
                break
            
            # This looks like a team name
            if len(line) > 2 and not odds_regex.search(line):
                teams.insert(0, line)
                if len(teams) >= 2:
                    break
        
        # Create event name
        if len(teams) >= 2:
            event_name = f"{teams[0]} vs {teams[1]}"
        elif len(teams) == 1:
            event_name = teams[0]
        else:
            event_name = "Unknown"
        
        # Create selections
        for line_num, label, odds in group:
            name = f"{event_name} - {label}"
            selections.append(ExtractedSelection(name=name, odds=odds))
    
    return ExtractedMarket(
        market_name=rule.market_name,
        selections=selections,
    )


def _extract_unlabeled_grouped_odds(
    lines: List[str],
    rule: SmartRule,
    odds_regex: re.Pattern,
    selection_labels: List[str],
) -> ExtractedMarket:
    """
    Extract odds that appear in consecutive groups without labels.
    Structure: TeamA / TeamB / Time / 4/11 / 3/1 / 5/1
    """
    selections = []
    
    # Find all valid odds positions
    odds_positions = []
    for i, line in enumerate(lines):
        line_stripped = line.strip()
        if odds_regex.fullmatch(line_stripped) and _is_valid_odds(line_stripped):
            odds_positions.append((i, line_stripped))
    
    # Group odds that are close together (within 4 lines of each other)
    # This handles both consecutive odds and odds with gaps (labels, spaces)
    groups = []
    current_group = []
    
    for i, (pos, odds) in enumerate(odds_positions):
        if not current_group:
            current_group = [(pos, odds)]
        elif pos - current_group[-1][0] <= 4:  # Within 4 lines
            current_group.append((pos, odds))
        else:
            if len(current_group) >= 2:
                groups.append(current_group)
            current_group = [(pos, odds)]
    
    if len(current_group) >= 2:
        groups.append(current_group)
    
    # For each group, find the teams/event before it
    time_pattern = re.compile(r'^\d{1,2}:\d{2}$')
    date_pattern = re.compile(r'^\d{1,2}\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)', re.IGNORECASE)
    date_words = {'today', 'tomorrow', 'yesterday', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'}
    
    for group in groups:
        first_odds_line = group[0][0]
        
        # Look back to find team names
        teams = []
        for j in range(first_odds_line - 1, max(0, first_odds_line - 12), -1):
            line = lines[j].strip()
            if not line:
                continue
            
            lower_line = line.lower()
            
            # Skip time, date, labels, numbers
            if (time_pattern.match(line) or 
                date_pattern.match(line) or
                lower_line in date_words or
                lower_line in {'1', 'x', '2', 'stats'} or
                line.isdigit()):
                continue
            
            # Skip junk
            if any(skip in lower_line for skip in ['times backed', 'bet builder', 'add to', 'player to']):
                break  # Stop looking, we've gone too far
            
            # This looks like a team name
            if len(line) > 2 and not odds_regex.search(line):
                teams.insert(0, line)  # Insert at beginning since we're going backwards
                if len(teams) >= 2:
                    break
        
        # Create selections for this group
        if len(teams) >= 2:
            event_name = f"{teams[0]} vs {teams[1]}"
            
            for idx, (pos, odds) in enumerate(group):
                if idx < len(selection_labels):
                    label = selection_labels[idx]
                    name = f"{event_name} - {label}"
                else:
                    name = f"{event_name} - Selection {idx + 1}"
                
                selections.append(ExtractedSelection(name=name, odds=odds))
        elif len(teams) == 1:
            # Single team/event found
            for idx, (pos, odds) in enumerate(group):
                if idx < len(selection_labels):
                    name = f"{teams[0]} - {selection_labels[idx]}"
                else:
                    name = f"{teams[0]} - Selection {idx + 1}"
                
                selections.append(ExtractedSelection(name=name, odds=odds))
    
    return ExtractedMarket(
        market_name=rule.market_name,
        selections=selections,
    )


def save_smart_rule(domain: str, rule: SmartRule, rules_dir: str = "rules") -> str:
    """Save a smart rule to JSON file."""
    os.makedirs(rules_dir, exist_ok=True)
    
    filename = f"{domain}_smart_rule.json"
    filepath = os.path.join(rules_dir, filename)
    
    data = {
        "market_name": rule.market_name,
        "odds_pattern": rule.odds_pattern,
        "name_position": rule.name_position,
        "name_separator": rule.name_separator,
        "sample_odds": rule.sample_odds,
        "description": rule.description,
        "group_size": rule.group_size,
        "market_type": rule.market_type,
        "selection_labels": rule.selection_labels,
        "start_marker": rule.start_marker,
        "end_marker": rule.end_marker,
    }
    
    with open(filepath, "w") as f:
        json.dump(data, f, indent=2)
    
    logger.info("Saved smart rule to %s", filepath)
    return filepath


def load_smart_rule(domain: str, rules_dir: str = "rules") -> Optional[SmartRule]:
    """Load a smart rule from JSON file."""
    filename = f"{domain}_smart_rule.json"
    filepath = os.path.join(rules_dir, filename)
    
    if not os.path.exists(filepath):
        return None
    
    with open(filepath, "r") as f:
        data = json.load(f)
    
    return SmartRule(
        market_name=data["market_name"],
        odds_pattern=data["odds_pattern"],
        name_position=data["name_position"],
        name_separator=data.get("name_separator", " "),
        sample_odds=data.get("sample_odds", []),
        description=data.get("description", ""),
        group_size=data.get("group_size", 3),
        market_type=data.get("market_type", ""),
        selection_labels=data.get("selection_labels", []),
        start_marker=data.get("start_marker", ""),
        end_marker=data.get("end_marker", ""),
    )
