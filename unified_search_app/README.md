# Unified Search App — Docupedia + Viva

Search both Docupedia (Confluence) and Viva Engage in one command.

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Basic search (returns combined results)
python main.py --search "copilot" --max-results 10

# 3. Search Docupedia only
python main.py --search "implementation" --source docupedia

# 4. Search Viva only
python main.py --search "ai" --source viva
```

## Output
Results saved to: `data/output/unified_search_results.json`

## Architecture

```
unified_search_app/
├── main.py                    # CLI entry point
├── requirements.txt           # Dependencies
├── config/
│   └── config.yaml           # Search settings
├── search/
│   ├── __init__.py
│   ├── docupedia_adapter.py  # Docupedia search wrapper
│   ├── viva_adapter.py       # Viva search wrapper
│   └── unified_searcher.py   # Orchestrates both searches
├── data/
│   └── output/               # Results saved here
└── logs/
    └── search.log            # Search logs
```

## Features

✅ Search both platforms simultaneously
✅ Combined JSON output with relevance scores
✅ Fallback to local crawling if needed
✅ CLI with flexible filtering
✅ Detailed logging
