# Unified Search App — Quick Start Guide

## Setup (One-time)

### 1. Activate Conda Environment
```bash
conda activate py311
```

### 2. Install Dependencies
```bash
cd unified_search_app
pip install -r requirements.txt
```

All packages are likely already installed in py311.

---

## Basic Usage

### Search Both Platforms (Docupedia + Viva)
```bash
python main.py --search "keyword" --max-results 10
```

### Search Docupedia Only
```bash
python main.py --search "python" --source docupedia --max-results 5
```

### Search Viva Only (requires authentication)
```bash
python main.py --search "copilot" --source viva --max-results 20
```

---

## CLI Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--search` | string | **REQUIRED** | Keyword to search for |
| `--max-results` | int | 20 | Results per platform (Viva max: 50) |
| `--source` | choice | both | Platform: `both`, `docupedia`, `viva` |
| `--parallel` | flag | True | Search both platforms simultaneously |
| `--sequential` | flag | False | Search platforms one by one |
| `--timeout` | int | 60 | Timeout in seconds per search |
| `--output` | path | data/output/... | Where to save JSON results |
| `--verbose` | flag | False | Enable debug logging |

---

## Output Format

Results saved to: `data/output/unified_search_results.json`

```json
{
  "query": "copilot",
  "timestamp": "2026-09-17T13:56:19.610307",
  "total_results": 8,
  "search_duration_seconds": 0.15,
  "docupedia_results": [
    {
      "source": "Docupedia",
      "space_key": "FARM",
      "space_name": "FARM",
      "page_id": "562171306",
      "page_title": "👨‍💻 Code Examples",
      "chunk_id": "confluence_562171306:0",
      "content_snippet": "...",
      "content_length": 36245,
      "relevance_score": 0.9,
      "url": null
    }
  ],
  "viva_results": [
    {
      "source": "Viva Engage",
      "group_name": "AI & Automation",
      "thread_id": "xyz123",
      "starter_name": "John Doe",
      "starter_email": "john.doe@company.com",
      "post_text": "Discussion about copilot...",
      "reactions": 5,
      "reply_count": 3,
      "created_at": "2026-09-15T14:00:00Z",
      "relevance_score": 1.0
    }
  ],
  "errors": []
}
```

---

## Examples

### Example 1: Quick Search for "AI"
```bash
python main.py --search "ai" --max-results 10
```

Output will show top 10 results from Docupedia (if any) and Viva threads.

### Example 2: Deep Docupedia Search
```bash
python main.py --search "machine learning" --source docupedia --max-results 20
```

Get detailed results from Docupedia pages & chunks.

### Example 3: Sequential Search (slower but safer)
```bash
python main.py --search "python" --sequential --timeout 120
```

Search Docupedia first, then Viva. Gives more time if network is slow.

### Example 4: Verbose Debug
```bash
python main.py --search "api" --verbose
```

Shows detailed logs in `logs/search.log` and console.

---

## Troubleshooting

### Issue: "No module named 'jarvis'" (Viva search skipped)
**Solution:** This is expected if Viva Crawler is not properly configured. Docupedia search still works.

### Issue: No Docupedia results found
**Check:** 
1. Ensure `../Docupedia crawler/data/raw/` has chunk files
2. Try more common keywords
3. Run with `--verbose` to see debug logs

### Issue: Unicode output error on Windows
**Solution:** Already fixed! The app now handles UTF-8 emoji characters.

---

## Architecture

```
unified_search_app/
├── main.py                      # CLI entry point
├── requirements.txt             # Dependencies
├── config/
│   └── config.yaml             # Search settings
├── search/
│   ├── models.py               # Data models (DocupediaResult, VivaResult)
│   ├── utils.py                # Config loading, logging
│   ├── docupedia_adapter.py    # Searches Docupedia chunks
│   ├── viva_adapter.py         # Searches Viva Engage
│   └── unified_searcher.py     # Orchestrates both searches
├── data/
│   └── output/                 # Results saved here
└── logs/
    └── search.log              # Detailed logs
```

### How It Works

1. **CLI Parse** → User provides search query and options
2. **Initialize Adapters** → Load Docupedia chunks & Viva authentication
3. **Parallel Search** → Search both platforms simultaneously (if enabled)
4. **Combine Results** → Merge and rank results
5. **Save JSON** → Output to `data/output/unified_search_results.json`
6. **Display Summary** → Print results to console

---

## Advanced Usage

### Custom Output Path
```bash
python main.py --search "test" --output my_results/search_20260917.json
```

### Combine With Tools
```bash
# Save results and show top results
python main.py --search "copilot" > search_log.txt
```

### Integrate Into Scripts
```python
from search.unified_searcher import UnifiedSearcher
from search.utils import load_config

searcher = UnifiedSearcher()
results = searcher.search("python", max_results=15, source="docupedia")
print(f"Found {results.total_results} results")
```

---

## Performance Notes

| Platform | Speed | Limit | Notes |
|----------|-------|-------|-------|
| **Docupedia** | <100ms | 1000s chunks | Fast local search |
| **Viva** | 1-5s | 50 results | Requires OAuth, network dependent |
| **Combined** | 1-5s | Both limits | Parallel search is default |

---

## Next Steps (Future Enhancements)

- [ ] Add vector embeddings for semantic search (not just keyword matching)
- [ ] Add ranking/sorting by relevance or date
- [ ] Support for advanced filters (by date, space, group)
- [ ] Web UI for search
- [ ] Integration with ChatGPT for smarter summaries
- [ ] Scheduled crawling for Docupedia updates

---

**Created:** 2026-09-17  
**Status:** ✅ Working and Tested
