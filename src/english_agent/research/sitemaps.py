"""Per-site URL index built from sitemaps and cached on disk.

None of the three sites exposes a usable search endpoint, so "search" means
scanning article URLs from their sitemaps. englex publishes a sitemap index with
~170 monthly post sitemaps; skyeng/skysmart publish flat sitemaps.
"""

from __future__ import annotations

import json
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from english_agent.research.sites import Site

_SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


def _locs(root: ET.Element, parent_tag: str) -> list[str]:
    return [
        loc.text.strip()
        for node in root.iter(f"{_SITEMAP_NS}{parent_tag}")
        if (loc := node.find(f"{_SITEMAP_NS}loc")) is not None and loc.text
    ]


class SitemapIndex:
    def __init__(self, http: httpx.Client, cache_dir: Path, ttl_days: int = 7, workers: int = 8) -> None:
        self._http = http
        self._cache_dir = cache_dir
        self._ttl_seconds = ttl_days * 24 * 3600
        self._workers = workers
        self._memory: dict[str, list[str]] = {}

    def urls_for(self, site: Site) -> list[str]:
        if site.name in self._memory:
            return self._memory[site.name]
        cached = self._read_cache(site)
        if cached is None:
            cached = self._build(site)
            self._write_cache(site, cached)
        self._memory[site.name] = cached
        return cached

    # -- cache -----------------------------------------------------------------

    def _cache_path(self, site: Site) -> Path:
        return self._cache_dir / "sitemaps" / f"{site.name}.json"

    def _read_cache(self, site: Site) -> list[str] | None:
        path = self._cache_path(site)
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - payload.get("fetched_at", 0) > self._ttl_seconds:
            return None
        return list(payload["urls"])

    def _write_cache(self, site: Site, urls: list[str]) -> None:
        path = self._cache_path(site)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"fetched_at": time.time(), "urls": urls}), encoding="utf-8")

    # -- building --------------------------------------------------------------

    def _fetch_xml(self, url: str) -> ET.Element | None:
        try:
            response = self._http.get(url)
            response.raise_for_status()
            return ET.fromstring(response.content)
        except (httpx.HTTPError, ET.ParseError):
            return None

    def _build(self, site: Site) -> list[str]:
        leaf_roots: list[ET.Element] = []
        child_sitemaps: list[str] = []
        for entry in site.sitemaps:
            root = self._fetch_xml(entry)
            if root is None:
                continue
            children = _locs(root, "sitemap")
            if not children:
                leaf_roots.append(root)  # flat sitemap: already have the URLs
                continue
            if site.child_sitemap_filter:
                children = [child for child in children if site.child_sitemap_filter in child]
            child_sitemaps.extend(children)

        with ThreadPoolExecutor(max_workers=self._workers) as pool:
            leaf_roots.extend(root for root in pool.map(self._fetch_xml, child_sitemaps) if root is not None)

        urls: set[str] = set()
        for root in leaf_roots:
            urls.update(url for url in _locs(root, "url") if site.is_article(url))
        return sorted(urls)
