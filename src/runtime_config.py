import json
import os
from pathlib import Path
from typing import Any, Mapping, Optional


WINDOWS_DEFAULT_SELENIUM_REMOTE_URL = "http://szh2vm0372.apac.bosch.com:4444/wd/hub"
LINUX_DEFAULT_SELENIUM_REMOTE_URL = "http://172.17.0.1:4444/wd/hub"


def load_global_config(project_root: Path) -> dict[str, Any]:
    config_path = project_root / "config" / "global.json"
    if not config_path.exists():
        return {}

    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[CONFIG] Failed to load global config {config_path}: {e}")
        return {}

    if not isinstance(payload, dict):
        print(f"[CONFIG] Global config root must be JSON object: {config_path}")
        return {}

    return payload


def get_platform_default_selenium_remote_url(os_name: Optional[str] = None) -> str:
    normalized_os = (os_name or os.name).strip().lower()
    if normalized_os == "nt":
        return WINDOWS_DEFAULT_SELENIUM_REMOTE_URL
    return LINUX_DEFAULT_SELENIUM_REMOTE_URL


def resolve_selenium_remote_url(
    *,
    explicit_override: Optional[str] = None,
    global_config: Optional[Mapping[str, Any]] = None,
    environ: Optional[Mapping[str, str]] = None,
    os_name: Optional[str] = None,
) -> tuple[str, str]:
    runtime_value = str(explicit_override or "").strip()
    if runtime_value:
        return runtime_value, "runtime-override"

    env_map = environ or os.environ
    env_value = str(env_map.get("SELENIUM_REMOTE_URL", "")).strip()
    if env_value:
        return env_value, "environment"

    if global_config is not None:
        global_value = str(global_config.get("selenium_remote_url", "")).strip()
        if global_value:
            return global_value, "global-config"

    return get_platform_default_selenium_remote_url(os_name=os_name), "platform-default"


def get_deprecated_site_selenium_remote_url(site_config: Mapping[str, Any]) -> str:
    return str(site_config.get("selenium_remote_url", "")).strip()