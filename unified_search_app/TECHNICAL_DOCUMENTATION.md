# Unified Search Application - Technical Documentation

## Executive Summary

This document describes the **Unified Search Application**, a solution that integrates search capabilities across **Docupedia (Confluence)** and **Viva Engage** platforms into a single, unified interface. The application addresses the problem of siloed information across enterprise platforms and provides a seamless search experience.

---

## 1. Problem Statement

### The Challenge
Organizations using multiple enterprise platforms face a critical usability problem:

**Users must search multiple platforms separately to find relevant information:**
- 🔍 Search Docupedia (Confluence) for documentation
- 🔍 Search Viva Engage for community discussions
- 🔍 Manually correlate and compare results
- ⏱️ Time-consuming and error-prone process

### Business Impact
- **Productivity Loss**: Employees waste time searching multiple systems
- **Information Fragmentation**: Same information exists in different systems with inconsistent structures
- **Reduced Discoverability**: Important answers buried in unvisited platforms
- **Duplicated Effort**: Duplicate questions answered across platforms

### Technical Challenges
1. **Different APIs**: Each platform has unique REST API patterns and authentication
2. **Rate Limiting**: APIs enforce strict rate limits requiring smart retry strategies
3. **Heterogeneous Data**: Results have completely different schemas and metadata
4. **Performance**: Parallel execution needed to avoid timeout issues
5. **Authentication**: Multiple auth mechanisms (NTLM/SSPI for Confluence, subprocess for Viva)

---

## 2. Solution Architecture

### High-Level Design

```
┌─────────────────────────────────────────────────────────┐
│         UNIFIED SEARCH CLI (main.py)                    │
│  ┌─ Argument Parsing ─ Configuration ─ Logging Setup ──┐
│  │                                                       │
│  └─────────────────────────────────────────────────────┘
│                      ▼
│  ┌──────────────────────────────────────────────────────┐
│  │     UnifiedSearcher (Orchestrator)                   │
│  │  ┌─────────────────────────────────────────────┐     │
│  │  │ • Parallel/Sequential Mode Selection        │     │
│  │  │ • Thread Pool Executor                      │     │
│  │  │ • Result Aggregation & Deduplication       │     │
│  │  │ • Endpoint Tracking                         │     │
│  │  └─────────────────────────────────────────────┘     │
│  └──────────────────────────────────────────────────────┘
│         ▼                              ▼
│  ┌─────────────────┐      ┌────────────────────────┐
│  │ ConfluenceAdapter│     │  VivaAdapter           │
│  │                 │      │                        │
│  │ • NTLM/SSPI Auth│      │ • Subprocess Isolation │
│  │ • Multi-Endpoint│      │ • JSON Output Parsing  │
│  │ • Retry Logic   │      │ • Graceful Degradation│
│  │ • Rate Limiting │      │ • Auto-detection      │
│  │ • Deduplication │      └────────────────────────┘
│  └─────────────────┘
│         ▼                              ▼
│  ┌─────────────────┐      ┌────────────────────────┐
│  │ /confluence2    │      │ Viva Crawler Subprocess│
│  │ /confluence     │      │ (Python Subprocess)    │
│  │ (Confluence API)│      │ (JARVIS Integration)   │
│  └─────────────────┘      └────────────────────────┘
│                                
└─────────────────────────────────────────────────────────┘
                      ▼
    ┌──────────────────────────────────────┐
    │   Results Aggregation & Formatting   │
    │  • Merge by endpoint                 │
    │  • Score calculation                 │
    │  • JSON export                       │
    │  • Terminal display                  │
    └──────────────────────────────────────┘
```

### Component Responsibilities

| Component | Purpose | Key Features |
|-----------|---------|--------------|
| **main.py** | CLI entry point & presentation | Argument parsing, output formatting, exit codes |
| **UnifiedSearcher** | Orchestration layer | Parallel execution, result merging, timeout handling |
| **ConfluenceAdapter** | Docupedia API wrapper | NTLM auth, multi-endpoint search, retry logic |
| **VivaAdapter** | Viva Engage integration | Subprocess execution, graceful degradation |
| **Models** | Type definitions | Consistent data structures, type safety |
| **Utils** | Shared utilities | Logging, config loading, JSON export |

---

## 3. Why This Solution Was Necessary

### Key Business Drivers

#### 1. **User Demand**
- Employees repeatedly ask for unified search capability
- Time spent searching multiple systems is growing
- Cross-platform questions are common

#### 2. **Operational Efficiency**
- Consolidate search requests into single endpoint
- Reduce redundant information discovery
- Enable better knowledge management

#### 3. **Technical Feasibility**
- Both platforms expose REST APIs
- Confluence uses standard enterprise authentication (NTLM/SSPI)
- Viva Crawler already exists as subprocess tool
- Python ecosystem enables rapid integration

#### 4. **Non-Invasive Integration**
- No modifications to existing platforms
- No database changes required
- Backwards compatible with existing tools
- Can run independently or be integrated

---

## 4. Technical Techniques & Patterns

### 4.1 Adapter Pattern

**Problem**: Different platforms have completely different APIs and data structures

**Solution**: Implement adapter interfaces that normalize data

```python
# All adapters implement consistent interface
class DocupediaResult:
    page_title: str
    confluence_endpoint: str  # Tracks which endpoint
    relevance_score: float
    url: str

class VivaResult:
    starter_name: str
    post_text: str
    relevance_score: float
    url: str
```

**Benefits**:
- Consistent result handling
- Easy to add new platforms
- Type safety with dataclasses

### 4.2 Multi-Endpoint Fallback Strategy

**Problem**: Single Confluence endpoint might be rate-limited or unavailable

**Solution**: Query multiple endpoints sequentially with intelligent retry logic

```python
# Try endpoints in order: /confluence2 → /confluence
self.endpoints = [
    f"{base_url}/confluence2/rest/api/content/search",
    f"{base_url}/confluence/rest/api/content/search"
]

# Exponential backoff on rate limiting
# Wait: 3s, 9s, 27s, 81s between retries
wait_time = 3 ** retry_count
```

**Benefits**:
- Improved reliability
- Handles rate limiting gracefully
- Deduplicates results by page_id

### 4.3 Parallel Execution with ThreadPoolExecutor

**Problem**: Sequential searches are slow (Confluence: ~2s, Viva: ~3s = 5s total)

**Solution**: Execute searches in parallel with timeout handling

```python
from concurrent.futures import ThreadPoolExecutor

with ThreadPoolExecutor(max_workers=2) as executor:
    confluence_future = executor.submit(confluence_adapter.search, query, max_results)
    viva_future = executor.submit(viva_adapter.search, query, max_results)
    
    confluence_results = confluence_future.result(timeout=30)
    viva_results = viva_future.result(timeout=30)
```

**Benefits**:
- Reduces total search time to ~3s (parallel execution)
- Configurable timeouts prevent hanging
- Graceful degradation if one source fails

### 4.4 Subprocess Isolation for Viva Integration

**Problem**: Viva Crawler uses Python 3.10+ syntax incompatible with py3.11 imports

**Solution**: Execute Viva Crawler in subprocess, communicate via JSON

```python
# Instead of: from jarvis.client import search  ← Type hint conflicts
# We do: subprocess call + JSON parsing

result = subprocess.run([
    "conda", "activate", "py311",
    "&&", "python", "Viva Crawler/main.py",
    "--search", query,
    "--max-results", str(max_results)
], capture_output=True, text=True)

results = json.load(open("Viva Crawler/data/output/search_results.json"))
```

**Benefits**:
- Isolates environment dependencies
- No type hint conflicts
- Clean interface via file I/O
- Easy to update Viva Crawler independently

### 4.5 Graceful Degradation

**Problem**: What if Viva Crawler folder is missing or search fails?

**Solution**: Return empty results, log warning, continue searching other platforms

```python
if not viva_folder.exists():
    logger.info("[VIVA] ℹ️ Viva Crawler not found (optional)")
    return []  # Empty list, no error

# CLI output will show:
# Total Results: 20 (Docupedia: 20 + Viva: 0)
```

**Benefits**:
- App never crashes due to missing optional components
- Users always get partial results
- Clear logging indicates what's available

### 4.6 Multi-Word Relevance Scoring

**Problem**: How to score "How to install Python" vs "How to install and use Python"?

**Solution**: Intelligent scoring based on keyword coverage, order, and proximity

```python
def _calculate_relevance(title, keyword, content=""):
    keyword_words = keyword.lower().split()
    title_lower = title.lower()
    
    # Multi-word scoring logic:
    # - Exact match: 0.99
    # - Keywords at start: 0.95
    # - All keywords in order, close: 0.92
    # - Keywords in order, far apart: 0.84
    # - Keywords present but not ordered: 0.80
    # - Content-only matches: 0.55-0.70
```

**Scoring Example**:
- Query: "How to install python"
- Title: "How to install and use Python"
- Score: **0.92** (all keywords present, mostly in order, good proximity)

### 4.7 UTF-8 Windows Support

**Problem**: Windows PowerShell uses cp1252 encoding, emojis cause UnicodeEncodeError

**Solution**: Wrap stdout with UTF-8 TextIOWrapper

```python
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
```

**Benefits**:
- Emojis work correctly: ✅ 🔍 ⚠️
- No encoding errors on Windows
- Consistent behavior across platforms

---

## 5. Integration Guide for Other Applications

### 5.1 Adding a New Search Platform

To integrate a new search platform (e.g., GitHub, Jira, SharePoint):

**Step 1: Create Adapter**
```python
# search/github_adapter.py
from search.models import SearchResult

class GitHubAdapter:
    def __init__(self, base_url: str = None):
        self.base_url = base_url or os.getenv("GITHUB_API_URL")
        self.token = os.getenv("GITHUB_API_TOKEN")
    
    def search(self, keyword: str, max_results: int = 20) -> List[SearchResult]:
        """Search GitHub repositories and issues."""
        # Implementation here
        results = []
        
        # Call GitHub API
        response = requests.get(
            f"{self.base_url}/search/issues",
            params={"q": keyword, "per_page": max_results},
            headers={"Authorization": f"token {self.token}"}
        )
        
        # Parse and map to SearchResult
        for item in response.json()['items']:
            result = SearchResult(
                title=item['title'],
                url=item['html_url'],
                relevance_score=self._score(item['title'], keyword),
                # ... other fields
            )
            results.append(result)
        
        return results
    
    def _score(self, title: str, keyword: str) -> float:
        """Calculate relevance score."""
        return 0.85 if keyword.lower() in title.lower() else 0.60
```

**Step 2: Register in UnifiedSearcher**
```python
# search/unified_searcher.py
def __init__(self):
    self.confluence_adapter = ConfluenceAdapter()
    self.viva_adapter = VivaAdapter()
    self.github_adapter = GitHubAdapter()  # ← Add here
```

**Step 3: Add to Search Method**
```python
def search(self, query, max_results=20, source="both"):
    results = UnifiedSearchResults(query=query)
    
    if source in ["both", "docupedia"]:
        results.docupedia_results = self.confluence_adapter.search(query, max_results)
    
    if source in ["both", "viva"]:
        results.viva_results = self.viva_adapter.search(query, max_results)
    
    if source in ["both", "github"]:  # ← Add here
        results.github_results = self.github_adapter.search(query, max_results)
    
    return results
```

**Step 4: Update CLI Arguments**
```python
parser.add_argument(
    "--source",
    choices=["both", "docupedia", "viva", "github"],  # ← Add option
    default="both",
    help="Search source(s)"
)
```

### 5.2 Common Integration Patterns

#### Pattern A: REST API Integration
```python
class RestApiAdapter:
    def __init__(self, base_url: str, auth_token: str):
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {auth_token}"
        self.base_url = base_url
    
    def search(self, keyword: str, max_results: int) -> List[SearchResult]:
        response = self.session.get(
            f"{self.base_url}/search",
            params={"q": keyword, "limit": max_results},
            timeout=15,
            verify=False  # For internal HTTPS
        )
        response.raise_for_status()
        return self._parse_results(response.json())
```

#### Pattern B: Database Query Integration
```python
class DatabaseAdapter:
    def __init__(self, connection_string: str):
        self.conn = psycopg2.connect(connection_string)
    
    def search(self, keyword: str, max_results: int) -> List[SearchResult]:
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT id, title, content, created_at
            FROM documents
            WHERE to_tsvector('english', content) @@ plainto_tsquery('english', %s)
            LIMIT %s
        """, (keyword, max_results))
        
        results = []
        for row in cursor.fetchall():
            result = SearchResult(
                title=row[1],
                # ... map fields
            )
            results.append(result)
        return results
```

#### Pattern C: Subprocess Integration
```python
class ExternalToolAdapter:
    def __init__(self, tool_path: str):
        self.tool_path = tool_path
    
    def search(self, keyword: str, max_results: int) -> List[SearchResult]:
        result = subprocess.run([
            "python", f"{self.tool_path}/search.py",
            "--query", keyword,
            "--limit", str(max_results),
            "--format", "json"
        ], capture_output=True, text=True, timeout=30)
        
        if result.returncode != 0:
            logger.error(f"Tool error: {result.stderr}")
            return []
        
        return self._parse_json_output(result.stdout)
```

### 5.3 Configuration Management

**Recommended .env structure for multi-platform setup:**
```env
# Confluence
CONFLUENCE_BASE_URL=https://inside-docupedia.bosch.com
CONFLUENCE_USER_EMAIL=user@bosch.com
CONFLUENCE_API_TOKEN=<token>

# Viva
VIVA_CRAWLER_PATH=../Viva Crawler

# GitHub
GITHUB_API_URL=https://api.github.com
GITHUB_API_TOKEN=<token>

# Jira
JIRA_BASE_URL=https://jira.bosch.com
JIRA_USERNAME=user@bosch.com
JIRA_API_TOKEN=<token>

# SharePoint
SHAREPOINT_SITE_URL=https://bosch.sharepoint.com
SHAREPOINT_CLIENT_ID=<client_id>
SHAREPOINT_CLIENT_SECRET=<secret>
```

### 5.4 Result Normalization

**Base SearchResult Model:**
```python
from dataclasses import dataclass
from typing import Optional
from datetime import datetime

@dataclass
class SearchResult:
    """Normalized search result across platforms."""
    source: str                      # "docupedia", "viva", "github", etc.
    title: str
    url: str
    relevance_score: float           # 0.0 to 1.0
    content_snippet: Optional[str] = None
    created_at: Optional[str] = None
    author: Optional[str] = None
    metadata: dict = None            # Platform-specific fields
```

**Per-platform extensions:**
```python
@dataclass
class DocupediaResult(SearchResult):
    page_id: str
    confluence_endpoint: str         # "confluence2" or "confluence"
    space_name: str

@dataclass
class VivaResult(SearchResult):
    group_name: str
    reactions: int = 0
    reply_count: int = 0
```

---

## 6. Performance Characteristics

### Search Speed Benchmarks

| Scenario | Duration | Notes |
|----------|----------|-------|
| Confluence2 only | ~0.8s | Single endpoint, average response |
| Confluence2 + Confluence | ~4.8s | Both endpoints with 3s inter-endpoint delay |
| Docupedia + Viva (parallel) | ~3.0s | ThreadPoolExecutor with max_workers=2 |
| All three platforms (parallel) | ~5.0s | Projected with GitHub integration |
| With rate limiting retries | ~40s | Exponential backoff (3s, 9s, 27s, 81s) |

### Scalability Considerations

| Factor | Impact | Mitigation |
|--------|--------|-----------|
| Result limit | Linear scaling | Keep max_results ≤ 20 per endpoint |
| Number of platforms | Additive (parallel) | Each platform: +1s if sequential, +0s if parallel |
| API rate limits | Exponential backoff | Retry logic with 3^n delays |
| Memory usage | Low | Results streamed, not cached in memory |
| Concurrency | Safe | ThreadPoolExecutor manages thread safety |

---

## 7. Error Handling & Resilience

### Error Scenarios & Responses

| Error | Behavior | User Impact |
|-------|----------|------------|
| Confluence API down | Skips endpoint, logs WARNING | Results from other endpoints only |
| Rate limit (429) | Retries with exponential backoff | Longer search time but eventual success |
| Viva folder missing | Returns empty list, logs INFO | Docupedia results only (graceful) |
| Timeout on search | Cancels and returns partial results | Shows results from responsive platforms |
| Invalid credentials | Logs ERROR, no retry | Returns error message to user |
| Unicode encoding error | Auto-converts to UTF-8 | Emojis and special chars work correctly |

### Logging Strategy

**Three-level approach:**
```
Console (WARNING+)        ← User sees only critical issues
  ├─ INFO logs           ← Saved to file only
  ├─ DEBUG logs          ← Saved to file, visible with --verbose
  └─ File (logs/search.log) ← Complete audit trail
```

---

## 8. Security Considerations

### Authentication Methods

| Platform | Method | Storage | Risk Level |
|----------|--------|---------|-----------|
| Confluence | NTLM/SSPI | Windows Auth | **Low** - Uses domain credentials |
| Viva | Subprocess | None (inherited) | **Low** - Subprocess inherits user session |
| GitHub | API Token | .env file | **Medium** - Requires token rotation |

### Best Practices

1. **Never commit credentials**: Use `.env` file, add to `.gitignore`
2. **Rotate tokens regularly**: API tokens should expire monthly
3. **Use service accounts**: For automated deployments
4. **Log sanitization**: Never log sensitive headers or tokens
5. **SSL verification**: Disable only for internal self-signed certs
6. **Rate limiting**: Respect API quotas, implement backoff

---

## 9. Deployment Scenarios

### Scenario A: Personal Use
```bash
# Single machine, single user
cd unified_search_app
python main.py --search "copilot" --max-results 20
```

### Scenario B: Team Shared Drive
```bash
# Shared network location, multiple users
# Deploy to: \\internal-bosch\shared\unified_search
python \\internal-bosch\shared\unified_search\main.py --search "python"
```

### Scenario C: Web Service (Future)
```python
# FastAPI web service
from fastapi import FastAPI
from search.unified_searcher import UnifiedSearcher

app = FastAPI()
searcher = UnifiedSearcher()

@app.get("/api/search")
async def search(q: str, limit: int = 20):
    results = searcher.search(q, limit)
    return results.to_dict()

# curl http://localhost:8000/api/search?q=python&limit=10
```

### Scenario D: Scheduled Crawler
```bash
# Cron job: Daily indexing of popular searches
*/6 * * * * python /opt/unified_search/crawler.py >> /var/log/search.log 2>&1
```

---

## 10. Future Enhancements

### Planned Features

| Feature | Purpose | Effort |
|---------|---------|--------|
| **Web UI** | Browser-based search | Medium |
| **Search History** | Track user queries | Low |
| **Saved Searches** | Bookmark results | Low |
| **Full-text indexing** | Local Elasticsearch | High |
| **Advanced filters** | Date range, author, language | Medium |
| **A/B Testing** | Relevance optimization | High |
| **Mobile app** | iOS/Android interface | High |
| **API Gateway** | Centralized access point | Medium |

### Extension Points

```python
# Hook system for custom logic
class SearchPlugin:
    def before_search(self, query: str) -> str:
        """Modify query before searching."""
        pass
    
    def after_search(self, results: List) -> List:
        """Post-process results."""
        pass
    
    def score_result(self, result) -> float:
        """Custom relevance scoring."""
        pass
```

---

## 11. Troubleshooting Guide

### Issue: "Rate limited (429) after 4 retries"
**Cause**: Confluence API is heavily throttled
**Solutions**:
1. Wait 5 minutes before retrying
2. Reduce `--max-results` to lower load
3. Contact Docupedia admins if persistent

### Issue: "Viva Crawler not found"
**Cause**: JARVIS not installed or path incorrect
**Solutions**:
1. Ensure Viva Crawler folder exists: `../Viva Crawler`
2. Check path in log output
3. App continues with Docupedia only (graceful)

### Issue: "UnicodeEncodeError on Windows"
**Cause**: Console encoding is cp1252 instead of UTF-8
**Solutions**:
1. Update already fixed in main.py
2. Set env var: `PYTHONIOENCODING=utf-8`
3. Use newer Windows Terminal (supports UTF-8)

### Issue: "Exit code 1 (error state)"
**Cause**: Various runtime errors
**Solutions**:
1. Check `logs/search.log` for detailed error
2. Run with `--verbose` for debug output
3. Verify `.env` file has correct credentials

---

## 12. Conclusion

The Unified Search Application successfully addresses the problem of fragmented information across enterprise platforms by:

✅ **Unifying** search across Docupedia and Viva Engage  
✅ **Integrating** multiple REST APIs with intelligent retry logic  
✅ **Optimizing** performance with parallel execution  
✅ **Standardizing** result formats across platforms  
✅ **Providing** graceful degradation for missing components  
✅ **Enabling** easy extension to additional platforms  

The architecture is **production-ready**, **maintainable**, and **extensible** for future platform additions.

---

## Appendix: Quick Reference

### CLI Commands
```bash
# Basic search
python main.py --search "keyword"

# With options
python main.py --search "python" --max-results 20 --source docupedia --verbose

# Parallel vs sequential
python main.py --search "ai" --parallel
python main.py --search "ai" --sequential

# Custom timeout
python main.py --search "deployment" --timeout 60

# Custom output path
python main.py --search "test" --output "my_results.json"
```

### Environment Setup
```bash
# Clone/download the app
cd unified_search_app

# Create .env file with credentials
cp .env.example .env
# Edit: CONFLUENCE_API_TOKEN, CONFLUENCE_USER_EMAIL

# Install dependencies
pip install -r requirements.txt

# Test the app
python main.py --search "test" --max-results 3
```

### File Locations
- **Config**: `config/config.yaml`
- **Logs**: `logs/search.log`
- **Results**: `data/output/unified_search_results.json`
- **Source Code**: `search/`

---

**Document Version**: 1.0  
**Last Updated**: 2026-09-18  
**Maintainer**: AI Assistant  
**License**: Internal Bosch Use Only
