# Unified Search App — Docupedia + Viva

Search both Docupedia (Confluence) and Viva Engage in one command.

## Setup

### Prerequisites

- Python
- Windows environment (for Viva Engage SSO via jarvis)
- Bosch internal network access

### Configuration

Create a personal API token: https://id.atlassian.com/manage-profile/security/api-tokens

Add to `.env` file in the `unified_search_app/` directory:

```bash
CONFLUENCE_BASE_URL=https://inside-docupedia.bosch.com
CONFLUENCE_API_TOKEN=your_api_token_here
CONFLUENCE_USER_EMAIL=your.email@bosch.com
```

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
│   ├── models.py             # Data model definitions
│   ├── confluence_adapter.py # Docupedia search adapter
│   ├── viva_adapter.py       # Viva Engage search adapter
│   ├── unified_searcher.py   # Orchestrates both searches
│   └── utils.py              # Utility functions
├── data/
│   ├── output/               # Search results (JSON)
│   └── state/                # Application state
├── logs/
│   └── search.log            # Execution logs
└── jarvis/                    # Auth module (bundled)
    └── ...                    # Viva authentication
```

## Features

- ✅ **Dual-source search** - Query both Docupedia and Viva simultaneously
- ✅ **Rich Docupedia data** - Full page content, attachments, version history, labels
- ✅ **Complete Viva integration** - Direct jarvis GraphQL API access with proper URLs
- ✅ **Structured JSON output** - All results with relevance scores and metadata
- ✅ **Attachment support** - Download links for all page attachments with positions
- ✅ **Multi-endpoint support** - Searches both Confluence and Confluence2 automatically
- ✅ **Flexible filtering** - Control results per platform independently
- ✅ **Detailed logging** - Track search execution and errors
- ✅ **Windows SSO** - Automatic authentication via jarvis for Viva

## Data Structure

### Docupedia Result
```json
{
  "source": "Docupedia",
  "page_id": "610051146",
  "page_title": "How to install and use Python",
  "space_key": "PSEPCVN",
  "space_name": "PS/EPC-VN",
  "url": "https://inside-docupedia.bosch.com/confluence2/spaces/PSEPCVN/pages/610051146/...",
  "full_body": "<div>...complete HTML content...</div>",
  "body_storage": "<ac:layout>...Confluence storage format...</ac:layout>",
  "version": 5,
  "status": "current",
  "labels": ["tutorial"],
  "attachments": [
    {
      "id": "610051211",
      "title": "image.png",
      "media_type": "image/png",
      "download_link": "https://inside-docupedia.bosch.com/confluence2/download/attachments/610051146/image.png?..."
    }
  ],
  "relevance_score": 0.92,
  "content_length": 19005
}
```

### Viva Result
```json
{
  "source": "Viva Engage",
  "thread_id": "eyJfdHlwZSI6IlRocmVhZCIsImlkIjoiMzgxMTQ3ODc3MDU0MDU0NSJ9",
  "group_name": "DevCorner",
  "starter_name": "Reister Hunny (BD/TOA-SAM1)",
  "starter_email": "hansjoerg.reister@de.bosch.com",
  "post_text": "...",
  "url": "https://engage.cloud.microsoft/main/threads/eyJfdHlwZSI6IlRocmVhZCIsImlkIjoiMzgxMTQ3ODc3MDU0MDU0NSJ9?search=keyword",
  "reactions": 17,
  "reply_count": 0,
  "created_at": "2026-04-14T07:47:18.608Z",
  "relevance_score": 0.17
}
```

## Environment

Tested with:
- Python 3.11 (Conda py311)
- Windows 10/11
- Anaconda / Conda environments

## Requirements

See `requirements.txt` for all dependencies. Main packages:
- requests
- requests-negotiate-sspi (Confluence authentication)
- pyyaml (configuration)
- jarvis (Viva authentication & GraphQL)

## Usage Examples

### Search with different result limits
```bash
python main.py --search "python installation" --docupedia-results 5 --viva-results 10
```

### Sequential search (slower but less resource-intensive)
```bash
python main.py --search "ai" --sequential
```

### Verbose logging
```bash
python main.py --search "copilot" --verbose
```

## Notes

- Docupedia results show top-scoring page from each endpoint (Confluence + Confluence2)
- Viva results are sorted by relevance score (based on reactions)
- All URLs are clickable and ready to use
- Attachment download links are pre-constructed with full paths
- Search results are cached in `data/output/unified_search_results.json`
