import json
from pathlib import Path
from typing import Any, Dict


class ConfigError(Exception):
    pass


def load_config(config_path: str) -> Dict[str, Any]:
    path = Path(config_path)
    if not path.exists():
        raise ConfigError(f"Config file not found: {config_path}")

    with path.open("r", encoding="utf-8") as f:
        config = json.load(f)

    required_base_keys = ["base_url", "login"]
    missing_base = [k for k in required_base_keys if k not in config]
    if missing_base:
        raise ConfigError(f"Missing required keys in config: {', '.join(missing_base)}")

    has_legacy_flow = all(k in config for k in ["navigation_steps", "scrape", "output"])
    has_tasks_flow = isinstance(config.get("tasks"), list) and len(config.get("tasks", [])) > 0

    if not has_legacy_flow and not has_tasks_flow:
        raise ConfigError(
            "Config must provide either legacy flow keys (navigation_steps/scrape/output) "
            "or a non-empty tasks array."
        )

    # Attach metadata so workflow factory can dispatch site-specific implementations.
    config["__site_name"] = path.parent.name
    config["__config_path"] = str(path)
    return config
