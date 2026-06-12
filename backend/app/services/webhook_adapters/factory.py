from .base import WebhookAdapter
from .github_adapter import GitHubAdapter
from .generic_adapter import GenericAdapter
from .jira_adapter import JiraAdapter

_ADAPTERS = {
    "github": GitHubAdapter,
    "jira": JiraAdapter,
    "generic": GenericAdapter,
}


class WebhookAdapterFactory:
    @staticmethod
    def create(source: str) -> WebhookAdapter:
        return _ADAPTERS.get(source, GenericAdapter)()

    @staticmethod
    def sources() -> list[str]:
        return list(_ADAPTERS.keys())
