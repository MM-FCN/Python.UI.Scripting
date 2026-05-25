from typing import Any, Dict

from src.workflow.base_workflow import WorkflowCrawler


class Site2WorkflowCrawler(WorkflowCrawler):
    """Site2-specific workflow crawler.

    Keep only site2 business-flow overrides here.
    Shared captcha handling stays in the base workflow for reuse across sites.
    """

    def __init__(self, config: Dict[str, Any], headless: bool = False) -> None:
        super().__init__(config=config, headless=headless)
