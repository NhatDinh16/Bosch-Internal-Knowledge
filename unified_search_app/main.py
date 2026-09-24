#!/usr/bin/env python3
"""
Unified Search App — Search Docupedia and Viva Engage together.

Usage:
    python main.py --search "keyword" --max-results 10
    python main.py --search "python" --source viva
    python main.py --search "ai" --source docupedia
"""

import argparse
import sys
import json
import io
import os
from pathlib import Path

# Suppress SSL warnings for internal HTTPS
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Fix Unicode output on Windows
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

# Load environment variables from .env file
def load_env_file(env_path=None):
    """Load .env file into os.environ (no external dependency)."""
    if not env_path:
        env_path = Path(__file__).parent / ".env"
    
    env_path = Path(env_path)
    if not env_path.is_file():
        return
    
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("\"'")
            if key:
                os.environ.setdefault(key, value)

# Load .env before importing anything that uses environment variables
load_env_file()

from search.utils import load_config, setup_logging, save_results_json
from search.unified_searcher import UnifiedSearcher


def main():
    """Main CLI entry point."""
    # Load config to get default values
    config = load_config()
    default_docupedia_results = config.get('docupedia', {}).get('max_results', 20)
    default_viva_results = config.get('viva', {}).get('max_results', 10)
    
    parser = argparse.ArgumentParser(
        description="Unified Search — Search Docupedia and Viva Engage",
        epilog="Example: python main.py --search 'copilot' --docupedia-results 20 --viva-results 15"
    )
    
    parser.add_argument(
        "--search",
        type=str,
        required=True,
        help="Search keyword or phrase"
    )
    
    parser.add_argument(
        "--max-results",
        type=int,
        default=None,
        help="Maximum results per platform (overrides config values)"
    )
    
    parser.add_argument(
        "--docupedia-results",
        type=int,
        default=None,
        help=f"Maximum Docupedia results (default from config: {default_docupedia_results})"
    )
    
    parser.add_argument(
        "--viva-results",
        type=int,
        default=None,
        help=f"Maximum Viva results (default from config: {default_viva_results})"
    )
    
    parser.add_argument(
        "--source",
        choices=["both", "docupedia", "viva"],
        default="both",
        help="Which platform to search (default: both)"
    )
    
    parser.add_argument(
        "--parallel",
        action="store_true",
        default=True,
        help="Search both platforms in parallel (default: True)"
    )
    
    parser.add_argument(
        "--sequential",
        action="store_true",
        help="Search platforms sequentially instead of parallel"
    )
    
    parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="Search timeout in seconds (default: 60)"
    )
    
    parser.add_argument(
        "--output",
        type=str,
        default="data/output/unified_search_results.json",
        help="Output file path (default: data/output/unified_search_results.json)"
    )
    
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logging"
    )
    
    args = parser.parse_args()
    
    # Determine final result limits (CLI > --max-results > config defaults)
    if args.max_results:
        # --max-results overrides both CLI-specific and config
        docupedia_results = args.max_results
        viva_results = args.max_results
    else:
        # Use CLI-specific args or config defaults
        docupedia_results = args.docupedia_results if args.docupedia_results else default_docupedia_results
        viva_results = args.viva_results if args.viva_results else default_viva_results
    
    # Setup logging
    log_level = "DEBUG" if args.verbose else "INFO"
    logger = setup_logging(log_file="logs/search.log", level=log_level)
    
    logger.info("=" * 60)
    logger.info("UNIFIED SEARCH — Docupedia + Viva Engage")
    logger.info("=" * 60)
    logger.info(f"Query: '{args.search}'")
    logger.info(f"Source: {args.source}")
    logger.info(f"Docupedia results: {docupedia_results}")
    logger.info(f"Viva results: {viva_results}")
    logger.info(f"Parallel: {not args.sequential}")
    logger.info("=" * 60)
    
    try:
        # Initialize searcher
        searcher = UnifiedSearcher(use_parallel=not args.sequential)
        
        # Perform search with separate result limits
        results = searcher.search(
            query=args.search,
            max_results_docupedia=docupedia_results,
            max_results_viva=viva_results,
            source=args.source,
            timeout=args.timeout
        )
        
        # Print summary
        print("\n" + "=" * 70)
        print("UNIFIED SEARCH RESULTS - Docupedia + Viva Engage")
        print("=" * 70)
        print(f"Query: '{results.query}'")
        print(f"Total Results: {results.total_results} (Docupedia: {results.docupedia_count} + Viva: {results.viva_count})")
        print(f"Duration: {results.search_duration_seconds:.2f}s")
        if results.confluence_endpoints:
            print(f"Endpoints: {', '.join(results.confluence_endpoints).upper()}")
        
        if results.errors:
            print(f"\n⚠️  Errors ({len(results.errors)}):")
            for error in results.errors:
                print(f"  - {error}")
        
        # Print Docupedia results (highest score from each Confluence endpoint)
        if results.docupedia_results:
            print("\n" + "-" * 70)
            print("DOCUPEDIA RESULTS (Top Score Per Endpoint)")
            print("-" * 70)
            
            # Group by endpoint and get top result from each
            endpoints_best = {}
            for result in results.docupedia_results:
                ep = result.confluence_endpoint or 'unknown'
                if ep not in endpoints_best or result.relevance_score > endpoints_best[ep].relevance_score:
                    endpoints_best[ep] = result
            
            # Display top result from each endpoint
            for idx, ep in enumerate(sorted(endpoints_best.keys()), 1):
                result = endpoints_best[ep]
                print(f"\n[{idx}] {result.page_title}")
                print(f"    Score: {result.relevance_score:.2f} | Endpoint: {result.confluence_endpoint.upper()}")
                print(f"    Space: {result.space_name}")
                print(f"    Page ID: {result.page_id}")
                print(f"    URL: {result.url}")
        
        # Print Viva results (top N based on --viva-results parameter)
        if results.viva_results:
            print("\n" + "-" * 70)
            print(f"VIVA ENGAGE RESULTS (Top {len(results.viva_results)})")
            print("-" * 70)
            for i, result in enumerate(results.viva_results, 1):
                print(f"\n[{i}] {result.starter_name} in {result.group_name}")
                print(f"    Score: {result.relevance_score:.2f} | Created: {result.created_at}")
                print(f"    Reactions: {result.reactions} | Replies: {result.reply_count}")
                if result.post_text:
                    # Show first 200 chars of post
                    text = result.post_text[:200].replace('\n', ' ').replace('  ', ' ').strip()
                    if len(result.post_text) > 200:
                        text += "..."
                    print(f"    Post: {text}")
        
        print("\n" + "=" * 70)
        
        # Save results
        output_path = save_results_json(results.to_dict(), output_path=args.output)
        logger.info(f"Results saved to: {output_path}")
        print(f"✅ Results saved to: {output_path}")
        print("=" * 70)
        print()
        
    except KeyboardInterrupt:
        logger.info("Search interrupted by user")
        print("\n\n⚠️  Search interrupted by user.")
        sys.exit(0)
    
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        print(f"\n❌ Error: {e}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
    sys.exit(0)
