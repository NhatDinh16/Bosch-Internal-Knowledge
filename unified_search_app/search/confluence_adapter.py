"""Confluence API search adapter."""

import logging
import requests
import os
import time
from typing import List, Dict, Any, Optional
from requests_negotiate_sspi import HttpNegotiateAuth

from search.models import DocupediaResult

logger = logging.getLogger("unified_search.confluence_adapter")


class ConfluenceAdapter:
    """Search Confluence via REST API using NTLM/SSPI authentication."""
    
    def __init__(self, 
                 base_url: str = None,
                 use_sspi: bool = True):
        """
        Initialize Confluence adapter with SSPI/NTLM authentication.
        
        Args:
            base_url: Confluence base URL (auto-loads from env if None)
            use_sspi: Use NTLM/SSPI authentication (Kerberos)
        """
        # Load from environment if not provided
        if not base_url:
            base_url = os.getenv("CONFLUENCE_BASE_URL", "https://inside-docupedia.bosch.com")
        
        self.base_url = base_url.rstrip('/')
        
        # Confluence endpoints that work with Docupedia
        self.endpoints = [
            f"{self.base_url}/confluence2/rest/api/content/search",  # Confluence 2 (primary)
            f"{self.base_url}/confluence/rest/api/content/search",   # Confluence (may be rate-limited)
        ]
        
        # Use SSPI/NTLM authentication (Windows integrated auth)
        self.auth = HttpNegotiateAuth() if use_sspi else None
        
        # Track which endpoints were used in last search
        self.last_endpoints_used = []
        
        if self.auth:
            logger.info(f"[CONFLUENCE] ✅ Using SSPI/NTLM authentication")
            # Assume available if SSPI auth is configured
            self.available = True
        else:
            logger.warning("[CONFLUENCE] ⚠️ No authentication configured")
            self.available = False
    
    def _test_connection(self) -> bool:
        """Test if Confluence API is accessible."""
        try:
            response = requests.get(
                f"{self.base_url}/rest/api/space?limit=1",
                auth=self.auth,
                timeout=10,
                verify=False  # Skip SSL for internal URL
            )
            logger.debug(f"[CONFLUENCE] Connection test status: {response.status_code}")
            return response.status_code == 200
        except Exception as e:
            logger.debug(f"[CONFLUENCE] Connection test failed: {e}")
            return False
    
    def search(self, keyword: str, max_results: int = 20) -> List[DocupediaResult]:
        """
        Search Confluence using all available endpoints and merge results.
        Tries endpoints: /confluence2, /confluence, and direct.
        Deduplicates by page_id to avoid duplicates across endpoints.
        
        Args:
            keyword: Search term
            max_results: Max results to return per endpoint (total may be higher after merge)
            
        Returns:
            List of DocupediaResult objects (merged and deduplicated)
        """
        all_results = []
        seen_page_ids = set()  # Track page_ids to avoid duplicates
        endpoints_used = []
        
        if not self.available:
            logger.warning(f"[CONFLUENCE] API not available")
            self.last_endpoints_used = []
            return all_results
        
        try:
            logger.info(f"[CONFLUENCE] Starting search for: '{keyword}' (max {max_results} per endpoint)")
            
            # Build CQL query
            cql_query = f'text ~ "{keyword}"'
            
            params = {
                'cql': cql_query,
                'limit': max_results,
                'expand': 'space,body.view,body.storage,version,metadata.labels,children.attachment'  # Include all necessary fields
            }
            
            # Try each endpoint and collect results
            for idx, search_url in enumerate(self.endpoints):
                try:
                    # Add delay between endpoint requests to avoid rate limiting
                    if idx > 0:
                        logger.info(f"[CONFLUENCE] Waiting 3s before next endpoint (rate limit avoidance)")
                        time.sleep(3)
                    
                    logger.debug(f"[CONFLUENCE] Trying endpoint: {search_url}")
                    
                    # Retry logic for rate-limited endpoints
                    max_retries = 4
                    retry_count = 0
                    response = None
                    
                    while retry_count < max_retries:
                        try:
                            response = requests.get(
                                search_url,
                                params=params,
                                auth=self.auth,
                                timeout=15,
                                verify=False
                            )
                            
                            logger.debug(f"[CONFLUENCE] Response status: {response.status_code}")
                            
                            if response.status_code != 429:
                                break  # Success or other status - don't retry
                            
                            # Rate limited - wait and retry
                            retry_count += 1
                            if retry_count < max_retries:
                                wait_time = 3 ** retry_count  # Exponential backoff: 3s, 9s, 27s, 81s
                                logger.info(f"[CONFLUENCE] Rate limited (429). Retry {retry_count}/{max_retries-1} after {wait_time}s...")
                                time.sleep(wait_time)
                            else:
                                logger.warning(f"[CONFLUENCE] Rate limited (429) after {retry_count} retries. Giving up.")
                        
                        except requests.exceptions.Timeout:
                            logger.debug(f"[CONFLUENCE] Timeout on endpoint: {search_url}")
                            break
                        except Exception as e:
                            logger.debug(f"[CONFLUENCE] Error on endpoint {search_url}: {e}")
                            break
                    
                    if not response:
                        continue
                    
                    if response.status_code == 200:
                        logger.info(f"[CONFLUENCE] ✅ Got results from endpoint: {search_url}")
                        
                        # Extract endpoint name for logging (e.g., "confluence2", "confluence", or "direct")
                        # URL format: https://...base_url.../confluence2/rest/api/content/search
                        parts = search_url.split('/')
                        endpoint_name = parts[3] if len(parts) > 3 else "unknown"  # Get subdomain part after base URL
                        endpoints_used.append(endpoint_name)
                        
                        try:
                            data = response.json()
                        except Exception as e:
                            logger.error(f"[CONFLUENCE] Failed to parse JSON: {e}")
                            logger.error(f"[CONFLUENCE] Response (first 500 chars): {response.text[:500]}")
                            continue
                        
                        results_list = data.get('results', [])
                        logger.info(f"[CONFLUENCE] Found {len(results_list)} result(s) from {endpoint_name}")
                        
                        if len(results_list) == 0:
                            logger.debug(f"[CONFLUENCE] Empty results from {endpoint_name}")
                        
                        for item in results_list:
                            try:
                                # Extract page information
                                page_id = item.get('id', '')
                                title = item.get('title', '')
                                
                                if not page_id or not title:
                                    logger.debug(f"[CONFLUENCE] Skipping item: missing id or title")
                                    continue
                                
                                # Skip if we already have this page from another endpoint
                                if page_id in seen_page_ids:
                                    logger.debug(f"[CONFLUENCE] Skipping duplicate page_id: {page_id}")
                                    continue
                                
                                seen_page_ids.add(page_id)
                                
                                # Get space info
                                space_info = item.get('space', {})
                                space_key = space_info.get('key', 'UNKNOWN')
                                space_name = space_info.get('name', space_key)
                                
                                # Build URL - ensure it includes the correct endpoint
                                links = item.get('_links', {})
                                webui_path = links.get('webui', '')
                                
                                # Construct URL with proper endpoint path
                                if webui_path:
                                    # If webui_path already includes endpoint, use it directly
                                    if webui_path.startswith(f'/{endpoint_name}/'):
                                        url = f"{self.base_url}{webui_path}"
                                    # If webui_path doesn't include endpoint, add it
                                    elif webui_path.startswith('/'):
                                        url = f"{self.base_url}/{endpoint_name}{webui_path}"
                                    else:
                                        # Shouldn't happen, but handle gracefully
                                        url = f"{self.base_url}/{endpoint_name}/{webui_path}"
                                else:
                                    url = ""
                                
                                # Get body content (both view and storage formats)
                                body = item.get('body', {})
                                view_content = body.get('view', {}).get('value', '')
                                storage_content = body.get('storage', {}).get('value', '')
                                snippet = self._extract_snippet(view_content, keyword)
                                
                                # Extract additional metadata fields
                                version_info = item.get('version', {})
                                version_number = version_info.get('number', 0)
                                last_modified = version_info.get('when')
                                
                                # Extract status (current, archived, etc)
                                status = item.get('status', 'current')
                                
                                # Extract labels/tags
                                metadata = item.get('metadata', {})
                                labels_data = metadata.get('labels', {})
                                labels_list = labels_data.get('results', []) if isinstance(labels_data, dict) else []
                                label_names = [label.get('name', '') for label in labels_list]
                                
                                # Extract attachments
                                children = item.get('children', {})
                                attachments_data = children.get('attachment', {})
                                attachments_list = attachments_data.get('results', []) if isinstance(attachments_data, dict) else []
                                attachments = []
                                for att in attachments_list:
                                    download_link = att.get('_links', {}).get('download', '')
                                    # Construct full URL for attachment download link
                                    full_download_link = f"{self.base_url}/{endpoint_name}{download_link}" if download_link else ""
                                    
                                    attachment = {
                                        'id': att.get('id', ''),
                                        'title': att.get('title', ''),
                                        'media_type': att.get('metadata', {}).get('mediaType', ''),
                                        'download_link': full_download_link,
                                    }
                                    attachments.append(attachment)
                                
                                # Calculate relevance score (pass content for better scoring)
                                relevance = self._calculate_relevance(title, keyword, view_content)
                                
                                result = DocupediaResult(
                                    space_key=space_key,
                                    space_name=space_name,
                                    page_id=page_id,
                                    page_title=title,
                                    chunk_id=f"{page_id}:0",
                                    content_snippet=snippet,
                                    content_length=len(view_content),
                                    last_modified=last_modified,
                                    url=url,
                                    relevance_score=relevance,
                                    confluence_endpoint=endpoint_name,
                                    full_body=view_content,
                                    body_storage=storage_content,
                                    version=version_number,
                                    status=status,
                                    labels=label_names,
                                    attachments=attachments
                                )
                                all_results.append(result)
                            except Exception as e:
                                logger.error(f"[CONFLUENCE] Error parsing result: {e}")
                                logger.debug(f"[CONFLUENCE] Item that failed: {str(item)[:200]}")
                    
                    elif response.status_code == 401:
                        logger.debug("[CONFLUENCE] Authentication failed at this endpoint")
                        continue
                    
                    elif response.status_code == 403:
                        logger.debug(f"[CONFLUENCE] Access denied (403) at endpoint: {search_url}")
                        continue
                    
                    elif response.status_code == 429:
                        logger.warning(f"[CONFLUENCE] Rate limited (429) at endpoint: {search_url} (all retries exhausted)")
                        continue
                    else:
                        logger.debug(f"[CONFLUENCE] Unexpected status {response.status_code} at endpoint: {search_url}")
                        continue
                    
                except requests.exceptions.Timeout:
                    logger.debug(f"[CONFLUENCE] Timeout on endpoint: {search_url}")
                    continue
                except Exception as e:
                    logger.debug(f"[CONFLUENCE] Error on endpoint {search_url}: {e}")
                    continue
            
            # Return merged results from all endpoints
            if endpoints_used:
                logger.info(f"[CONFLUENCE] ✅ Search completed. Used {len(endpoints_used)} endpoint(s) ({', '.join(endpoints_used)})")
                logger.info(f"[CONFLUENCE] Total unique results: {len(all_results)}")
            else:
                logger.warning("[CONFLUENCE] No working endpoints found")
            
            # Store endpoints used for later retrieval
            self.last_endpoints_used = endpoints_used
            return all_results
            
        except Exception as e:
            logger.error(f"[CONFLUENCE] Search error: {e}")
            self.last_endpoints_used = []
            return all_results
    
    def _calculate_relevance(self, title: str, keyword: str, content: str = "") -> float:
        """
        Calculate relevance score based on multiple factors.
        
        Scoring logic:
        - Exact title match: 0.99
        - Multi-word: Score based on keyword coverage, order, and proximity
        - Single keyword at start: 0.95
        - Single keyword in title: 0.85-0.92 (based on position)
        - Content-based: 0.50-0.85 (based on frequency and density)
        """
        keyword_lower = keyword.lower()
        title_lower = title.lower()
        content_lower = content.lower() if content else ""
        
        # Exact match in title
        if keyword_lower == title_lower:
            return 0.99
        
        # Split keyword into words for multi-word phrase matching
        keyword_words = [w.strip() for w in keyword_lower.split() if w.strip()]
        title_words = [w.strip() for w in title_lower.split() if w.strip()]
        
        # If multi-word query, calculate coverage and proximity
        if len(keyword_words) > 1:
            # Count how many keywords appear in title
            keyword_set = set(keyword_words)
            title_set = set(title_words)
            matching_keywords = keyword_set.intersection(title_set)
            coverage = len(matching_keywords) / len(keyword_set)  # 0 to 1
            
            if coverage > 0:
                # Check if keywords are in order in the title
                keyword_positions = []
                for kw in keyword_words:
                    for i, tw in enumerate(title_words):
                        if kw == tw:
                            keyword_positions.append(i)
                            break
                
                # Score based on coverage and ordering
                if len(keyword_positions) == len(keyword_words):
                    # All keywords found, check if in order
                    in_order = all(keyword_positions[i] < keyword_positions[i+1] 
                                   for i in range(len(keyword_positions)-1))
                    
                    if in_order:
                        # Calculate proximity (words between matches)
                        gaps = [keyword_positions[i+1] - keyword_positions[i] 
                               for i in range(len(keyword_positions)-1)]
                        avg_gap = sum(gaps) / len(gaps) if gaps else 0
                        
                        # Score: 0.90-0.95 for in-order matches with small gaps
                        if avg_gap <= 2:
                            return 0.92  # Keywords close together
                        elif avg_gap <= 4:
                            return 0.88  # Keywords somewhat separated
                        else:
                            return 0.84  # Keywords far apart but in order
                    else:
                        # Keywords in title but not in order
                        return 0.80
                else:
                    # Only some keywords found, score by coverage
                    base_score = 0.70 + (coverage * 0.15)
                    return min(0.85, base_score)
        
        # Single keyword matching (original logic)
        # Keyword at beginning of title (very relevant)
        if title_lower.startswith(keyword_lower):
            return 0.95
        
        # Keyword in title (highly relevant)
        if keyword_lower in title_lower:
            # Score based on position - earlier = higher score
            pos = title_lower.find(keyword_lower)
            if pos < len(title_lower) / 4:  # First quarter
                return 0.92
            elif pos < len(title_lower) / 2:  # First half
                return 0.88
            else:  # Second half
                return 0.85
        
        # Count keyword occurrences in content
        if content_lower:
            occurrences = content_lower.count(keyword_lower)
            content_length = len(content_lower)
            
            if occurrences > 0:
                # Score based on frequency and relevance
                frequency_score = min(0.85, 0.5 + (occurrences * 0.05))
                
                # Bonus if content is well-matched (keyword frequency > 1%)
                if content_length > 0:
                    keyword_density = (occurrences * len(keyword_lower)) / content_length
                    if keyword_density > 0.01:
                        frequency_score = min(0.85, frequency_score + 0.05)
                
                return frequency_score
        
        # Fallback: keyword found via API but not in title or extracted content
        return 0.55
    
    def _extract_snippet(self, content: str, keyword: str, context_len: int = 100) -> str:
        """
        Extract snippet around keyword with context.
        
        Args:
            content: Full content text
            keyword: Search term
            context_len: Characters of context around keyword
            
        Returns:
            Snippet with ellipsis
        """
        if not content:
            return ""
        
        # Remove HTML tags for display
        import re
        clean_content = re.sub(r'<[^>]+>', '', content)
        
        keyword_lower = keyword.lower()
        content_lower = clean_content.lower()
        
        # Find first occurrence
        pos = content_lower.find(keyword_lower)
        
        if pos == -1:
            # Keyword not found, return start of content
            return clean_content[:200] + "..." if len(clean_content) > 200 else clean_content
        
        # Extract snippet with context
        start = max(0, pos - context_len)
        end = min(len(clean_content), pos + len(keyword) + context_len)
        
        snippet = clean_content[start:end]
        
        # Add ellipsis if needed
        if start > 0:
            snippet = "..." + snippet
        if end < len(clean_content):
            snippet = snippet + "..."
        
        return snippet[:300]  # Limit to 300 chars
