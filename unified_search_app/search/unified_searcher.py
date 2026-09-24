"""Unified searcher — orchestrates Confluence API and Viva searches."""

import logging
import time
from typing import List, Dict, Any, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from search.models import UnifiedSearchResults, DocupediaResult, VivaResult
from search.confluence_adapter import ConfluenceAdapter
from search.viva_adapter import VivaAdapter

logger = logging.getLogger("unified_search.orchestrator")


class UnifiedSearcher:
    """Orchestrates searches across both Docupedia and Viva."""
    
    def __init__(self, use_parallel: bool = True):
        """
        Initialize unified searcher.
        
        Args:
            use_parallel: Whether to search both platforms in parallel
        """
        self.use_parallel = use_parallel
        self.confluence = ConfluenceAdapter()
        self.viva = VivaAdapter()
        logger.info("[ORCHESTRATOR] Unified searcher initialized")
    
    def search(
        self,
        query: str,
        max_results_docupedia: int = None,
        max_results_viva: int = None,
        max_results: int = None,  # Fallback for backward compatibility
        source: str = "both",  # "both", "docupedia", "viva"
        timeout: int = 60
    ) -> UnifiedSearchResults:
        """
        Search both platforms for a query.
        
        Args:
            query: Search query/keyword
            max_results_docupedia: Max Docupedia results
            max_results_viva: Max Viva results
            max_results: Fallback max results per platform (for backward compatibility)
            source: Which platform(s) to search
            timeout: Timeout in seconds for each search
            
        Returns:
            UnifiedSearchResults with combined results
        """
        # Handle parameter resolution
        if max_results_docupedia is None:
            max_results_docupedia = max_results or 20
        if max_results_viva is None:
            max_results_viva = max_results or 20
        
        logger.info(
            f"[ORCHESTRATOR] Starting unified search: '{query}' "
            f"(source={source}, docupedia={max_results_docupedia}, viva={max_results_viva})"
        )
        
        start_time = time.time()
        results = UnifiedSearchResults(query=query)
        
        try:
            if self.use_parallel and source == "both":
                # Search both in parallel
                with ThreadPoolExecutor(max_workers=2) as executor:
                    futures = {}
                    
                    if source in ["both", "docupedia"]:
                        futures["docupedia"] = executor.submit(
                            self.confluence.search,
                            query,
                            max_results_docupedia
                        )
                    
                    if source in ["both", "viva"]:
                        futures["viva"] = executor.submit(
                            self.viva.search,
                            query,
                            max_results_viva
                        )
                    
                    # Collect results
                    for platform, future in futures.items():
                        try:
                            platform_results = future.result(timeout=timeout)
                            if platform == "docupedia":
                                results.docupedia_results = platform_results
                            elif platform == "viva":
                                results.viva_results = platform_results
                        except Exception as e:
                            logger.error(f"[ORCHESTRATOR] {platform} search failed: {e}")
                            results.errors.append(f"{platform}: {str(e)}")
            
            else:
                # Search sequentially
                if source in ["both", "docupedia"]:
                    try:
                        results.docupedia_results = self.confluence.search(query, max_results_docupedia)
                    except Exception as e:
                        logger.error(f"[ORCHESTRATOR] Confluence search failed: {e}")
                        results.errors.append(f"confluence: {str(e)}")
                
                if source in ["both", "viva"]:
                    try:
                        results.viva_results = self.viva.search(query, max_results_viva)
                    except Exception as e:
                        logger.error(f"[ORCHESTRATOR] Viva search failed: {e}")
                        results.errors.append(f"viva: {str(e)}")
        
        except Exception as e:
            logger.error(f"[ORCHESTRATOR] Unified search error: {e}", exc_info=True)
            results.errors.append(f"orchestrator: {str(e)}")
        
        # Extract confluence endpoint info from results
        if results.docupedia_results:
            results.confluence_endpoint = results.docupedia_results[0].confluence_endpoint
            # Get list of all unique endpoints used
            endpoints_set = {r.confluence_endpoint for r in results.docupedia_results if r.confluence_endpoint}
            results.confluence_endpoints = sorted(list(endpoints_set))
        
        # Calculate metrics
        results.total_results = results.docupedia_count + results.viva_count
        results.search_duration_seconds = time.time() - start_time
        
        logger.info(
            f"[ORCHESTRATOR] Search completed. "
            f"Docupedia: {results.docupedia_count}, "
            f"Viva: {results.viva_count}, "
            f"Duration: {results.search_duration_seconds:.2f}s"
        )
        
        return results
