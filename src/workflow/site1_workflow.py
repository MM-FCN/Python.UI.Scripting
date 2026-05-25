from typing import Any, Dict

from src.workflow.base_workflow import WorkflowCrawler


class Site1WorkflowCrawler(WorkflowCrawler):
    """Site1-specific workflow crawler.

    Keep site custom behavior here as site1 flow evolves.
    """

    def __init__(self, config: Dict[str, Any], headless: bool = False, params: Dict[str, str] = None) -> None:
        super().__init__(config=config, headless=headless, params=params)
