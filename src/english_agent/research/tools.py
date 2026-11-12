"""Tools available to the research subagent.

Both tools are plain functions behind a small `ResearchTools` protocol, so the
agent loop can be tested with an in-memory fake. Tool *schemas* (what the model
sees) live here too, next to the implementation they describe.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx
import trafilatura

from english_agent.research.models import PageContent, SearchHit, TopicNotes
from english_agent.research.sitemaps import SitemapIndex
from english_agent.research.sites import SITE_ORDER, Site, site_for_url

_NON_WORD = re.compile(r"[^a-z0-9]+")


def _slug_of(url: str) -> str:
    return urlparse(url).path.strip("/").rsplit("/", 1)[-1]


def _normalize(text: str) -> str:
    return _NON_WORD.sub(" ", text.lower()).strip()


MIN_KEYWORD_LENGTH = 3  # "a", "is", "to" match almost every slug and only add noise


def rank_urls(urls: list[str], keywords: list[str], limit: int) -> list[SearchHit]:
    """Score every URL by how many keywords occur in its slug. Pure, so it is unit-testable."""
    normalized_keywords = [
        normalized for keyword in keywords if len(normalized := _normalize(keyword)) >= MIN_KEYWORD_LENGTH
    ]
    if not normalized_keywords:
        return []
    hits: list[SearchHit] = []
    for url in urls:
        slug = _slug_of(url)
        haystack = f" {_normalize(slug)} "
        score = sum(1 for keyword in normalized_keywords if f" {keyword} " in haystack or keyword in haystack)
        if score:
            hits.append(SearchHit(url=url, slug=slug, score=score))
    hits.sort(key=lambda hit: (-hit.score, len(hit.slug), hit.url))
    return hits[:limit]


class ResearchTools(Protocol):  # утиная типизация (duck typing)
    async def search_site(self, site: Site, keywords: list[str], limit: int = 5) -> list[SearchHit]: ...

    async def fetch_page(self, url: str) -> PageContent: ...


def _extract(html: str, url: str) -> tuple[str, str]:
    """Main text and title of an article page. CPU-bound (lxml), so callers run it in a thread."""
    text = trafilatura.extract(html, include_comments=False, include_tables=True, favor_recall=True) or ""
    metadata = trafilatura.extract_metadata(html)
    title = (metadata.title if metadata and metadata.title else "").strip() or _slug_of(url)
    return text, title


class LiveResearchTools:
    def __init__(self, http: httpx.AsyncClient, index: SitemapIndex, page_char_limit: int) -> None:
        self._http = http
        self._index = index
        self._page_char_limit = page_char_limit

    async def search_site(self, site: Site, keywords: list[str], limit: int = 5) -> list[SearchHit]:
        return rank_urls(await self._index.urls_for(site), keywords, limit)

    async def fetch_page(self, url: str) -> PageContent:
        site = site_for_url(url)
        if site is None or not site.is_article(url):  # allowlist - ограничиваем агента
            raise ValueError(f"URL is outside the allowed sites: {url}")
        response = await self._http.get(url, follow_redirects=True)
        response.raise_for_status()
        # Parsing a 40k-char page takes tens of milliseconds: off the event loop, so other learners wait for nothing.
        text, title = await asyncio.to_thread(_extract, response.text, url)
        truncated = len(text) > self._page_char_limit
        return PageContent(url=url, title=title, text=text[: self._page_char_limit], truncated=truncated)


def tool_specs() -> list[dict[str, Any]]:
    """OpenAI-style function definitions. Kept in one place so prompt and code cannot drift."""
    return [
        {
            "type": "function",
            "function": {
                "name": "search_site",
                "description": (
                    "Find article URLs on ONE of the allowed sites whose slug mentions the keywords. "
                    "Sites must be tried strictly in order: englex, then skyeng, then skysmart."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "site": {"type": "string", "enum": list(SITE_ORDER)},
                        "keywords": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                            "maxItems": 12,
                            "description": (
                                "Short slug-style keywords. Include English grammar terms "
                                "(present, perfect, articles) AND Russian transliterations as used in "
                                "URL slugs (artikli, predlogi, glagol, vremya)."
                            ),
                        },
                    },
                    "required": ["site", "keywords"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "fetch_page",
                "description": "Download one article URL returned by search_site and return its main text.",
                "parameters": {
                    "type": "object",
                    "properties": {"url": {"type": "string"}},
                    "required": ["url"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "finish",
                "description": (
                    "Stop researching. If found=true, pass notes grounded in exactly one fetched page. "
                    "If found=false, you must have searched all three sites."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "found": {"type": "boolean"},
                        "notes": TopicNotes.model_json_schema(),
                    },
                    "required": ["found"],
                },
            },
        },
    ]
