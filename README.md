# Sports Betting Odds Scraper

Extract and monitor sports betting odds using natural language prompts.

## How It Works

```
┌─────────────────────────────────────────────────────────────────┐
│  1. SETUP (one-time per bookmaker)                              │
│                                                                 │
│     You: URL + "get me the 3 way money line odds"              │
│                        ↓                                        │
│     Fetch HTML → Claude analyzes → AutoScraper learns rules    │
│                        ↓                                        │
│     Rules saved to rules/<domain>.json                         │
└─────────────────────────────────────────────────────────────────┘
                         ↓
┌─────────────────────────────────────────────────────────────────┐
│  2. MONITOR (continuous, no Claude needed)                      │
│                                                                 │
│     Every 1 second:                                            │
│         AutoScraper → extracts odds → saves to JSONL file      │
│                                                                 │
│     Fast, cheap, no API calls                                  │
└─────────────────────────────────────────────────────────────────┘
```

## Installation

```bash
# Create virtual environment
python -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Set your Anthropic API key in .env
echo 'ANTHROPIC_API_KEY=sk-ant-...' > .env
```

### AI-driven live monitor

The live monitor uses OpenAI `gpt-5.6-luna` once at startup to analyze the
rendered page and create a data-only extraction plan. It validates that plan
against the page before polling, then uses the validated plan locally so the
model is not called for every odds tick.

```bash
export OPENAI_API_KEY="your_api_key_here"
python3 test_live_scraper.py \
  "https://www.pinnacle.com/en/tennis/matchups/live/" \
  --market moneyline
```

Odds are printed initially and again only when a player's price changes.

For a fully model-generated page-specific extractor, use:

```bash
python3 test_live_scraper.py \
  "https://www.pinnacle.com/en/tennis/matchups/live/" \
  --market moneyline
```

This writes the validated generated module to `generated_scraper.py`. If
generation, compilation, or extraction validation fails, the error is sent
back to GPT-5.6 Luna for another attempt, up to five attempts.

## Usage

### Step 1: Setup (one-time)

```bash
python main.py setup \
    --url "https://bookmaker.com/event/12345" \
    --prompt "get me the 3 way money line odds"
```

Claude analyzes the page, finds the odds, and trains AutoScraper.

### Step 2: Monitor (continuous)

```bash
python main.py monitor --url "https://bookmaker.com/event/12345"
```

Scrapes every second and saves to a JSONL file. No Claude API calls.

### Other Commands

```bash
# Single scrape
python main.py scrape --url "..." --raw

# List trained domains
python main.py list
```

## Output

Monitoring saves JSONL with timestamped snapshots:

```json
{"timestamp": "2026-01-22T10:30:01", "url": "...", "data": {"rule_1": ["Arsenal", "Chelsea", "2.10", "3.40"]}}
{"timestamp": "2026-01-22T10:30:02", "url": "...", "data": {"rule_1": ["Arsenal", "Chelsea", "2.15", "3.35"]}}
```

## Project Structure

```
scraper/
├── analyze.py    # Claude analyzes pages, proposes training examples
├── train.py      # AutoScraper training and rule storage
├── scrape.py     # Apply rules to extract data
├── monitor.py    # Continuous polling and recording
├── fetch.py      # HTTP requests
└── models.py     # Data models

main.py           # CLI interface
rules/            # Saved AutoScraper rules (per-domain)
```
