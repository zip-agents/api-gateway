"""Docs served by the zip services themselves (contract §14), fetched and cached here.

Each service publishes `GET /docs/manifest.json`, the markdown files it lists and,
optionally, its own OpenAPI document (manifest field `openapi`). The
gateway fetches them over the private network when a docs page needs them, renders them,
and caches the result, so what the portal shows always matches the build that is running.

Services are untrusted input as far as rendering goes: sizes and counts are capped, slugs
and paths are validated, and raw HTML in markdown is escaped. A service that is down, or
has not implemented §14, shows as "docs unavailable" and nothing else is affected.
"""

import asyncio
import json
import logging
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from markdown_it import MarkdownIt

from gateway.config import Upstream
from gateway.upstream import fetch

logger = logging.getLogger("gateway.service_docs")

MANIFEST_PATH = "/docs/manifest.json"
MAX_MANIFEST_BYTES = 64 * 1024
MAX_DOCUMENT_BYTES = 1024 * 1024
MAX_DOCUMENTS = 20
FETCH_TIMEOUT_SECONDS = 3.0
# Upper bound for one service's manifest plus all its documents; a page view waits for it.
LOAD_TIMEOUT_SECONDS = 10.0
CACHE_SECONDS = 300.0
# Failures are retried sooner, so a service that was restarting shows up quickly.
FAILURE_CACHE_SECONDS = 30.0

_SLUG = re.compile(r"^[a-z0-9-]{1,32}$")
_DOC_PATH = re.compile(r"^/docs/[A-Za-z0-9._-]{1,64}\.md$")
_SPEC_PATH = re.compile(r"^/docs/[A-Za-z0-9._-]{1,64}\.json$")
MAX_SPEC_BYTES = 1024 * 1024


class InvalidDocs(ValueError):
    pass


@dataclass(frozen=True)
class Document:
    slug: str
    title: str
    html: str


@dataclass(frozen=True)
class ServiceDocs:
    service: str
    version: str | None = None
    revision: str | None = None
    contract_version: str | None = None
    documents: dict[str, Document] = field(default_factory=dict)
    error: str | None = None  # set when the docs could not be fetched
    # The service's own OpenAPI document, exactly as published (None if it publishes none).
    spec: dict[str, Any] | None = None
    spec_error: str | None = None  # set when it lists one that could not be used

    @property
    def available(self) -> bool:
        return self.error is None


@dataclass(frozen=True)
class _Entry:
    docs: ServiceDocs
    expires: float


def parse_manifest(raw: bytes) -> tuple[dict[str, str | None], list[tuple[str, str, str]]]:
    """Validate a manifest; return (metadata, [(slug, title, path), ...])."""
    try:
        manifest = json.loads(raw)
    except ValueError as exc:
        raise InvalidDocs("manifest is not valid JSON") from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("documents"), list):
        raise InvalidDocs("manifest must be an object with a 'documents' list")
    metadata: dict[str, str | None] = {}
    for key in ("version", "revision", "contract_version"):
        value = manifest.get(key)
        metadata[key] = value[:64] if isinstance(value, str) else None
    spec_path = manifest.get("openapi")
    if spec_path is not None and (
        not isinstance(spec_path, str) or not _SPEC_PATH.fullmatch(spec_path)
    ):
        raise InvalidDocs(f"invalid openapi path: {spec_path!r}")
    metadata["openapi"] = spec_path
    entries: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for item in manifest["documents"][:MAX_DOCUMENTS]:
        if not isinstance(item, dict):
            raise InvalidDocs("each document must be an object")
        slug, title, path = item.get("slug"), item.get("title"), item.get("path")
        if not isinstance(slug, str) or not _SLUG.fullmatch(slug) or slug in seen:
            raise InvalidDocs(f"invalid or duplicate slug: {slug!r}")
        if not isinstance(path, str) or not _DOC_PATH.fullmatch(path):
            raise InvalidDocs(f"invalid document path: {path!r}")
        if not isinstance(title, str) or not title.strip():
            title = slug
        seen.add(slug)
        entries.append((slug, title.strip()[:80], path))
    return metadata, entries


def parse_spec(raw: bytes) -> dict[str, Any]:
    """Minimal validation of a service's OpenAPI document: enough to render and compare."""
    try:
        spec = json.loads(raw)
    except ValueError as exc:
        raise InvalidDocs("OpenAPI document is not valid JSON") from exc
    if (
        not isinstance(spec, dict)
        or not str(spec.get("openapi", "")).startswith("3.")
        or not isinstance(spec.get("paths"), dict)
    ):
        raise InvalidDocs("not an OpenAPI 3 document with paths")
    return spec


class ServiceDocsCache:
    def __init__(self, upstreams: Mapping[str, Upstream]) -> None:
        self._upstreams = upstreams
        self._entries: dict[str, _Entry] = {}
        # One fetch per service at a time; concurrent page views wait for it.
        self._locks = {name: asyncio.Lock() for name in upstreams}
        self._markdown = MarkdownIt("js-default")

    async def get(self, service: str) -> ServiceDocs:
        entry = self._entries.get(service)
        if entry is not None and entry.expires > time.monotonic():
            return entry.docs
        async with self._locks[service]:
            entry = self._entries.get(service)  # another request may have refreshed it
            if entry is not None and entry.expires > time.monotonic():
                return entry.docs
            docs = await self._load(self._upstreams[service])
            ttl = CACHE_SECONDS if docs.available else FAILURE_CACHE_SECONDS
            self._entries[service] = _Entry(docs, time.monotonic() + ttl)
            return docs

    async def get_all(self) -> list[ServiceDocs]:
        return list(await asyncio.gather(*(self.get(name) for name in self._upstreams)))

    async def _load(self, upstream: Upstream) -> ServiceDocs:
        try:
            async with asyncio.timeout(LOAD_TIMEOUT_SECONDS):
                raw = await self._fetch(upstream, MANIFEST_PATH, MAX_MANIFEST_BYTES)
                metadata, entries = parse_manifest(raw)
                bodies = await asyncio.gather(
                    *(self._fetch(upstream, path, MAX_DOCUMENT_BYTES) for _, _, path in entries)
                )
            documents = {
                slug: Document(slug, title, self._markdown.render(body.decode("utf-8", "replace")))
                for (slug, title, _), body in zip(entries, bodies, strict=True)
            }
        except Exception as exc:  # any failure: show "unavailable", never break the portal
            logger.info(
                "service docs unavailable",
                extra={"fields": {"service": upstream.name, "error": str(exc)}},
            )
            return ServiceDocs(service=upstream.name, error=_describe(exc))
        spec, spec_error = None, None
        spec_path = metadata["openapi"]
        if spec_path is not None:
            try:
                raw_spec = await self._fetch(upstream, spec_path, MAX_SPEC_BYTES)
                spec = parse_spec(raw_spec)
            except Exception as exc:  # the documents are still worth showing
                spec_error = _describe(exc)
                logger.info(
                    "service spec unavailable",
                    extra={"fields": {"service": upstream.name, "error": str(exc)}},
                )
        return ServiceDocs(
            service=upstream.name,
            version=metadata["version"],
            revision=metadata["revision"],
            contract_version=metadata["contract_version"],
            documents=documents,
            spec=spec,
            spec_error=spec_error,
        )

    @staticmethod
    async def _fetch(upstream: Upstream, path: str, limit: int) -> bytes:
        status, body = await fetch(upstream, "GET", path, FETCH_TIMEOUT_SECONDS, max_bytes=limit)
        if status != 200:
            raise InvalidDocs(f"GET {path} returned {status}")
        return body


def _describe(exc: Exception) -> str:
    if isinstance(exc, InvalidDocs):
        return str(exc)
    if isinstance(exc, TimeoutError):
        return "timed out"
    if isinstance(exc, OSError):
        return "service unreachable"
    return "invalid response"
