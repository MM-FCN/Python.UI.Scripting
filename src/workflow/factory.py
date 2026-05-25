from typing import Any, Dict

from src.workflow.base_workflow import WorkflowCrawler
from src.workflow.site1_workflow import Site1WorkflowCrawler
from src.workflow.site2_workflow import Site2WorkflowCrawler


def create_workflow_crawler(config: Dict[str, Any], headless: bool = False, params: Dict[str, str] = None) -> WorkflowCrawler:
    site_name = str(config.get("__site_name", "")).strip().lower()

    if site_name == "site1":
        return Site1WorkflowCrawler(config=config, headless=headless, params=params)
    if site_name == "site2":
        return Site2WorkflowCrawler(config=config, headless=headless, params=params)
    return WorkflowCrawler(config=config, headless=headless, params=params)
