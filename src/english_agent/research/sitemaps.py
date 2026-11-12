"""Per-site URL index built from sitemaps, cached so it is rebuilt at most once per TTL.

None of the three sites exposes a usable search endpoint, so "search" means
scanning article URLs from their sitemaps. englex publishes a sitemap index with
~170 monthly post sitemaps; skyeng/skysmart publish flat sitemaps.

The child sitemaps are fetched concurrently (bounded by a semaphore). Only a
complete index is cached: when any sitemap could not be fetched or parsed, the
URLs found are used for this run but not stored, so one network blip does not
leave a site half-searchable for SITEMAP_TTL_DAYS. The cache is a `SitemapCache`:
files on disk for command-line runs, Redis where several processes share it.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Protocol

import httpx

from english_agent.research.sites import Site

_SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


def _locs(root: ET.Element, parent_tag: str) -> list[str]:
    return [
        loc.text.strip()
        for node in root.iter(f"{_SITEMAP_NS}{parent_tag}")
        if (loc := node.find(f"{_SITEMAP_NS}loc")) is not None and loc.text
    ]


class SitemapCache(Protocol):
    async def get(self, site: str) -> list[str] | None: ...

    async def put(self, site: str, urls: list[str]) -> None: ...


class FileSitemapCache:
    """`<cache_dir>/sitemaps/<site>.json` with the fetch time; a stale or unreadable file counts as missing."""

    def __init__(self, cache_dir: Path, ttl_days: int = 7) -> None:
        self._dir = cache_dir / "sitemaps"
        self._ttl_seconds = ttl_days * 24 * 3600

    async def get(self, site: str) -> list[str] | None:
        return await asyncio.to_thread(self._read, site)

    async def put(self, site: str, urls: list[str]) -> None:
        await asyncio.to_thread(self._write, site, urls)

    def _read(self, site: str) -> list[str] | None:
        path = self._dir / f"{site}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if time.time() - payload.get("fetched_at", 0) > self._ttl_seconds:
            return None
        return list(payload["urls"])

    def _write(self, site: str, urls: list[str]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        # Write then rename: a reader in another process never sees a half-written file.
        fd, tmp = tempfile.mkstemp(dir=self._dir, prefix=f".{site}.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump({"fetched_at": time.time(), "urls": urls}, file)
        os.replace(tmp, self._dir / f"{site}.json")


class SitemapIndex:
    def __init__(self, http: httpx.AsyncClient, cache: SitemapCache, concurrency: int = 8) -> None:
        self._http = http
        self._cache = cache
        self._concurrency = concurrency
        self._memory: dict[str, list[str]] = {}

    async def urls_for(self, site: Site) -> list[str]:
        if site.name in self._memory:
            return self._memory[site.name]
        urls = await self._cache.get(site.name)
        if urls is None:
            urls, complete = await self._build(site)
            if complete and urls:
                await self._cache.put(site.name, urls)
        self._memory[site.name] = urls
        return urls

    # -- building --------------------------------------------------------------

    async def _fetch_xml(self, url: str, gate: asyncio.Semaphore) -> ET.Element | None:
        try:
            async with gate:
                response = await self._http.get(url)
            response.raise_for_status()
            return ET.fromstring(response.content)
        except (httpx.HTTPError, ET.ParseError):
            return None

    async def _build(self, site: Site) -> tuple[list[str], bool]:
        """All article URLs of the site, and whether every sitemap was read."""
        gate = asyncio.Semaphore(self._concurrency)
        complete = True
        leaf_roots: list[ET.Element] = []
        child_sitemaps: list[str] = []
        for root in await asyncio.gather(*(self._fetch_xml(entry, gate) for entry in site.sitemaps)):
            if root is None:
                complete = False
                continue
            children = _locs(root, "sitemap")
            if not children:
                leaf_roots.append(root)  # flat sitemap: already have the URLs
                continue
            if site.child_sitemap_filter:
                children = [child for child in children if site.child_sitemap_filter in child]
            child_sitemaps.extend(children)

        for root in await asyncio.gather(*(self._fetch_xml(child, gate) for child in child_sitemaps)):
            if root is None:
                complete = False
            else:
                leaf_roots.append(root)

        urls: set[str] = set()
        for root in leaf_roots:
            urls.update(url for url in _locs(root, "url") if site.is_article(url))
        return sorted(urls), complete
