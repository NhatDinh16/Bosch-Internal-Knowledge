"""Viva Engage search adapter - Direct jarvis integration."""

import logging
from typing import List, Optional
from pathlib import Path

from search.models import VivaResult

logger = logging.getLogger("unified_search.viva_adapter")


class VivaAdapter:
    """Viva Engage search using direct jarvis integration.
    
    Viva search is OPTIONAL:
    - If jarvis is available: uses direct GraphQL search
    - If jarvis not available: skips gracefully
    - Unified search works fine with Docupedia alone
    
    Advantages over subprocess:
    - No subprocess overhead
    - Faster execution
    - Reusable authenticated client
    - Better error handling
    """
    
    def __init__(self):
        """Initialize Viva adapter with jarvis EngageClient."""
        self.client = None
        self.available = False
        
        try:
            # Import jarvis EngageClient for GraphQL queries
            from jarvis.engage import EngageClient
            
            # Try to authenticate and create client
            logger.info("[VIVA] Attempting jarvis EngageClient authentication...")
            self.client = EngageClient()
            self.available = True
            logger.info("[VIVA] ✅ Jarvis EngageClient initialized successfully")
            
        except ImportError as e:
            logger.info(f"[VIVA] ℹ️ Jarvis not available: {e}")
            logger.info("[VIVA] ℹ️ Viva search will be skipped (optional)")
            self.available = False
            
        except Exception as e:
            logger.warning(f"[VIVA] ⚠️ Failed to initialize jarvis: {e}")
            logger.info("[VIVA] ℹ️ Viva search will be skipped (optional)")
            self.available = False
    
    def search(self, keyword: str, max_results: int = 10) -> List[VivaResult]:
        """Search Viva using direct jarvis GraphQL API.
        
        Args:
            keyword: Search term
            max_results: Maximum results to return (API limit: 50)
            
        Returns:
            List of VivaResult objects sorted by relevance
        """
        if not self.available or not self.client:
            logger.debug("[VIVA] Client not available, skipping Viva search")
            return []
        
        try:
            logger.info(f"[VIVA] Starting direct GraphQL search for: '{keyword}' (max {max_results})")
            
            # Clamp to API limit
            api_max = 50
            capped_results = min(max_results, api_max)
            if max_results > api_max:
                logger.info(f"[VIVA] API limit is {api_max} results, capping {max_results} → {capped_results}")
            
            # Call jarvis GraphQL search directly
            search_result = self.client.search(keyword, count=capped_results)
            
            if not search_result or not isinstance(search_result, dict):
                logger.warning(f"[VIVA] Invalid response from jarvis: {type(search_result)}")
                return []
            
            # Parse threads from response
            threads = search_result.get("threads", [])
            if not isinstance(threads, list):
                logger.warning(f"[VIVA] Unexpected threads format: {type(threads)}")
                return []
            
            # Convert threads to VivaResult objects
            results = []
            for thread in threads:
                try:
                    starter = thread.get("starter", {})
                    
                    # Extract optional fields with defaults
                    thread_id = thread.get("threadId", "")
                    sender_name = starter.get("sender", "Unknown")
                    sender_email = starter.get("senderEmail", "")
                    group_name = thread.get("group", "")
                    post_text = starter.get("text", "")
                    reactions = starter.get("reactionsTotal", 0)
                    created_at = thread.get("createdAt")
                    
                    # Generate Viva Engage URL
                    # Format: https://engage.cloud.microsoft/main/threads/{threadId}?search={keyword}
                    url = None
                    if thread_id:
                        from urllib.parse import quote
                        # URL encode the keyword for query parameter
                        encoded_keyword = quote(keyword)
                        url = f"https://engage.cloud.microsoft/main/threads/{thread_id}?search={encoded_keyword}"
                    
                    # Calculate relevance score based on reactions and recency
                    # Simple heuristic: normalize reactions to 0-1 range
                    relevance_score = min(1.0, reactions / 100.0) if reactions > 0 else 0.5
                    
                    result = VivaResult(
                        thread_id=thread_id,
                        starter_name=sender_name,
                        starter_email=sender_email,
                        group_name=group_name,
                        post_text=post_text,
                        reactions=reactions,
                        reply_count=thread.get("repliesCount", 0),
                        created_at=created_at,
                        url=url,
                        relevance_score=relevance_score
                    )
                    results.append(result)
                    
                except Exception as e:
                    logger.debug(f"[VIVA] Error parsing thread: {e}")
                    logger.debug(f"[VIVA] Problem thread data: {str(thread)[:200]}")
                    continue
            
            # Sort by relevance score (descending)
            results.sort(key=lambda r: r.relevance_score, reverse=True)
            
            logger.info(f"[VIVA] ✅ Found {len(results)} result(s) from jarvis GraphQL")
            return results[:max_results]
        
        except Exception as e:
            logger.error(f"[VIVA] ❌ Search error: {e}", exc_info=True)
            return []

