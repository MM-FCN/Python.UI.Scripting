from typing import Any, Dict

from src.workflow.base_workflow import WorkflowCrawler


class CargonaviWorkflowCrawler(WorkflowCrawler):
    """Cargonavi-specific workflow crawler."""

    def __init__(self, config: Dict[str, Any], headless: bool = False, params: Dict[str, str] = None) -> None:
        super().__init__(config=config, headless=headless, params=params)
