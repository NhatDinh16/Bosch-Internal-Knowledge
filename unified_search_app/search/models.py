"""Data models for unified search results."""

from dataclasses import dataclass, field, asdict
from typing import List, Optional, Dict, Any
from datetime import datetime


@dataclass
class DocupediaResult:
    """Single result from Docupedia (Confluence)."""
    source: str = "Docupedia"
    space_key: str = ""
    space_name: str = ""
    page_id: str = ""
    page_title: str = ""
    chunk_id: Optional[str] = None
    content_snippet: str = ""
    content_length: int = 0
    last_modified: Optional[str] = None
    url: Optional[str] = None
    relevance_score: float = 1.0
    confluence_endpoint: str = ""  # Which endpoint was used (e.g., /confluence2)
    # Additional fields from Docupedia crawler format
    full_body: str = ""  # Complete cleaned page body
    body_storage: str = ""  # Raw Confluence storage format
    version: int = 0  # Page version number
    status: str = "current"  # Page status (current, archived, etc)
    labels: List[str] = field(default_factory=list)  # Page labels/tags
    attachments: List[Dict[str, Any]] = field(default_factory=list)  # Page attachments with metadata
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class VivaResult:
    """Single result from Viva Engage."""
    source: str = "Viva Engage"
    group_name: str = ""
    thread_id: str = ""
    starter_name: str = ""
    starter_email: str = ""
    post_text: str = ""
    reactions: int = 0
    reply_count: int = 0
    created_at: Optional[str] = None
    url: Optional[str] = None
    relevance_score: float = 1.0
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class UnifiedSearchResults:
    """Combined search results from both platforms."""
    query: str
    total_results: int = 0
    search_duration_seconds: float = 0.0
    docupedia_results: List[DocupediaResult] = field(default_factory=list)
    viva_results: List[VivaResult] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    confluence_endpoint: str = ""  # Active Confluence endpoint used for search
    confluence_endpoints: List[str] = field(default_factory=list)  # All endpoints that returned results
    
    @property
    def docupedia_count(self) -> int:
        return len(self.docupedia_results)
    
    @property
    def viva_count(self) -> int:
        return len(self.viva_results)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "query": self.query,
            "timestamp": self.timestamp,
            "total_results": self.total_results,
            "search_duration_seconds": self.search_duration_seconds,
            "confluence_endpoint": self.confluence_endpoint,
            "confluence_endpoints": self.confluence_endpoints,
            "docupedia_results": [r.to_dict() for r in self.docupedia_results],
            "viva_results": [r.to_dict() for r in self.viva_results],
            "errors": self.errors
        }
