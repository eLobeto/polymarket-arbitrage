"""
config.py — Load and validate polymarket-arbitrage configuration.
"""

import os
import yaml
import logging
from pathlib import Path
from typing import Any, Dict

log = logging.getLogger("config")


def load_config(config_path: str = None) -> Dict[str, Any]:
    """
    Load YAML config and substitute environment variables.
    
    Args:
        config_path: Path to config.yaml (defaults to ../config/config.yaml)
    
    Returns:
        Dict of configuration
    """
    if config_path is None:
        config_path = Path(__file__).parent.parent / "config" / "config.yaml"
    
    if not Path(config_path).exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    
    with open(config_path) as f:
        config = yaml.safe_load(f)
    
    # Substitute environment variables (${VAR_NAME} syntax)
    config = _substitute_env_vars(config)
    
    log.info(f"✅ Loaded config from {config_path}")
    return config


def _substitute_env_vars(obj: Any) -> Any:
    """Recursively substitute ${ENV_VAR} placeholders in config."""
    if isinstance(obj, str):
        if obj.startswith("${") and obj.endswith("}"):
            env_var = obj[2:-1]
            value = os.getenv(env_var)
            if value is None:
                raise ValueError(f"Environment variable not set: {env_var}")
            return value
        return obj
    elif isinstance(obj, dict):
        return {k: _substitute_env_vars(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_substitute_env_vars(item) for item in obj]
    else:
        return obj


def validate_config(cfg: Dict[str, Any]) -> bool:
    """
    Validate required config keys.
    
    Args:
        cfg: Configuration dict
    
    Returns:
        True if valid, raises ValueError otherwise
    """
    required_keys = {
        "polygon": ["rpc_url", "chain_id"],
        "polymarket": ["clob_url", "usdc_contract"],
        "trading": ["target_combined_cost", "bankroll_usdc", "poll_interval_sec"],
    }
    
    for section, keys in required_keys.items():
        if section not in cfg:
            raise ValueError(f"Missing config section: {section}")
        for key in keys:
            if key not in cfg[section]:
                raise ValueError(f"Missing config key: {section}.{key}")
    
    log.info("✅ Config validation passed")
    return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config()
    validate_config(cfg)
    print("Config loaded and validated successfully!")
