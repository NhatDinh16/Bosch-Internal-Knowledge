"""Utilities — config loading, logging, etc."""

import yaml
import logging
import json
import sys
import io
from pathlib import Path
from typing import Dict, Any


def load_config(config_path: str = None) -> Dict[str, Any]:
    """Load YAML config file."""
    if config_path is None:
        config_path = Path(__file__).parent.parent / "config" / "config.yaml"
    else:
        config_path = Path(config_path)
    
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    
    with open(config_path, 'r') as f:
        config = yaml.safe_load(f) or {}
    
    return config


def setup_logging(log_file: str = "logs/search.log", level: str = "INFO") -> logging.Logger:
    """Configure logging with UTF-8 support for console output."""
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    
    logger = logging.getLogger("unified_search")
    logger.setLevel(getattr(logging, level))
    
    # File handler - log everything at INFO level
    fh = logging.FileHandler(log_path, encoding='utf-8')
    fh.setLevel(logging.INFO)
    
    # Console handler - only show WARNING and above unless DEBUG is requested
    ch = logging.StreamHandler()
    ch.setLevel(logging.WARNING if level == "INFO" else getattr(logging, level))
    
    # Fix for Windows cp1252 encoding issue with emojis
    if sys.stdout.encoding != 'utf-8':
        ch.stream = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    
    # Formatter
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    fh.setFormatter(formatter)
    ch.setFormatter(formatter)
    
    if not logger.handlers:
        logger.addHandler(fh)
        logger.addHandler(ch)
    
    return logger


def save_results_json(results: Dict[str, Any], output_path: str = "data/output/unified_search_results.json"):
    """Save search results to JSON file."""
    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    
    return str(output_file)
