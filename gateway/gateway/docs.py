"""The gateway's own markdown pages, rendered once at startup.

    docs/contract.md   the API contract every implementation follows (scripts/sync-contract.sh)
    docs/gateway.md    how to call the services through the gateway
    docs/openapi.yaml  the contract as an OpenAPI document (see openapi_view.py)

Each implementation's own docs are fetched from the service at runtime (service_docs.py).
Raw HTML in markdown is escaped (markdown-it's `js-default` preset).
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from markdown_it import MarkdownIt

DEFAULT_DOCS_DIR = Path(__file__).resolve().parent.parent / "docs"

_PAGES = (
    ("gateway", "gateway.md", "Using the gateway"),
    ("contract", "contract.md", "API contract"),
)


@dataclass(frozen=True)
class Page:
    slug: str
    title: str
    html: str


class DocsSite:
    def __init__(self, root: Path = DEFAULT_DOCS_DIR) -> None:
        self.root = root
        markdown = MarkdownIt("js-default")
        self.pages: dict[str, Page] = {}
        for slug, filename, title in _PAGES:
            path = root / filename
            if path.is_file():
                text = path.read_text(encoding="utf-8")
                self.pages[slug] = Page(slug, title, markdown.render(text))
        # The contract as an OpenAPI document; parsed once, so a broken file fails startup.
        self.contract_spec: dict[str, Any] = yaml.safe_load(
            self.openapi_path.read_text(encoding="utf-8")
        )

    @property
    def openapi_path(self) -> Path:
        return self.root / "openapi.yaml"
