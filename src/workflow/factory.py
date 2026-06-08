from typing import Any, Dict

from src.workflow.base_workflow import WorkflowCrawler
from src.workflow.site.cargonavi_workflow import CargonaviWorkflowCrawler
from src.workflow.site.cargo_workflow import CargoWorkflowCrawler


def create_workflow_crawler(config: Dict[str, Any], headless: bool = False, params: Dict[str, str] = None) -> WorkflowCrawler:
    site_name = str(config.get("__site_name", "")).strip().lower()

    if site_name == "cargonavi":
        return CargonaviWorkflowCrawler(config=config, headless=headless, params=params)
    if site_name == "cargo":
        return CargoWorkflowCrawler(config=config, headless=headless, params=params)
    return WorkflowCrawler(config=config, headless=headless, params=params)
