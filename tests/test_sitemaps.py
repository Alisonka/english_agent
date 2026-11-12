from __future__ import annotations

import json
import time
from pathlib import Path

import httpx

from english_agent.research.sitemaps import FileSitemapCache, SitemapIndex
from english_agent.research.sites import ENGLEX, SKYSMART

INDEX_XML = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://englex.ru/sitemap-pt-post-p1-2026-10.xml</loc></sitemap>
  <sitemap><loc>https://englex.ru/sitemap-pt-webinar-p1-2026-10.xml</loc></sitemap>
</sitemapindex>"""

POSTS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://englex.ru/present-perfect/</loc></url>
  <url><loc>https://englex.ru/articles-in-english/</loc></url>
</urlset>"""

SKYSMART_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://skysmart.ru/articles/english/present-simple-tense</loc></url>
  <url><loc>https://skysmart.ru/articles/study/how-to-focus</loc></url>
</urlset>"""


def make_client(routes: dict[str, str], calls: list[str]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        body = routes.get(str(request.url))
        if body is None:
            return httpx.Response(404)
        return httpx.Response(200, content=body.encode("utf-8"))

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_sitemap_index_follows_only_post_children(tmp_path: Path) -> None:
    calls: list[str] = []
    client = make_client(
        {
            "https://englex.ru/sitemap.xml": INDEX_XML,
            "https://englex.ru/sitemap-pt-post-p1-2026-10.xml": POSTS_XML,
        },
        calls,
    )
    index = SitemapIndex(client, FileSitemapCache(tmp_path))
    urls = await index.urls_for(ENGLEX)
    assert urls == ["https://englex.ru/articles-in-english/", "https://englex.ru/present-perfect/"]
    assert "https://englex.ru/sitemap-pt-webinar-p1-2026-10.xml" not in calls


async def test_flat_sitemap_filters_by_article_prefix_and_caches(tmp_path: Path) -> None:
    calls: list[str] = []
    client = make_client({SKYSMART.sitemaps[0]: SKYSMART_XML}, calls)
    index = SitemapIndex(client, FileSitemapCache(tmp_path))
    assert await index.urls_for(SKYSMART) == ["https://skysmart.ru/articles/english/present-simple-tense"]

    cache_file = tmp_path / "sitemaps" / "skysmart.json"
    assert cache_file.exists()
    fresh_index = SitemapIndex(client, FileSitemapCache(tmp_path))
    assert await fresh_index.urls_for(SKYSMART) == ["https://skysmart.ru/articles/english/present-simple-tense"]
    assert len(calls) == 1


async def test_expired_cache_is_rebuilt(tmp_path: Path) -> None:
    calls: list[str] = []
    client = make_client({SKYSMART.sitemaps[0]: SKYSMART_XML}, calls)
    cache_file = tmp_path / "sitemaps" / "skysmart.json"
    cache_file.parent.mkdir(parents=True)
    cache_file.write_text(json.dumps({"fetched_at": time.time() - 30 * 24 * 3600, "urls": ["stale"]}))

    index = SitemapIndex(client, FileSitemapCache(tmp_path, ttl_days=7))
    assert await index.urls_for(SKYSMART) == ["https://skysmart.ru/articles/english/present-simple-tense"]
    assert len(calls) == 1


async def test_an_incomplete_index_is_used_but_not_cached(tmp_path: Path) -> None:
    calls: list[str] = []
    # The index lists a post sitemap that answers 404: the URLs found are used, but not stored for a week.
    client = make_client({"https://englex.ru/sitemap.xml": INDEX_XML}, calls)
    index = SitemapIndex(client, FileSitemapCache(tmp_path))
    assert await index.urls_for(ENGLEX) == []
    assert not (tmp_path / "sitemaps" / "englex.json").exists()


async def test_an_unreadable_cache_file_counts_as_missing(tmp_path: Path) -> None:
    (tmp_path / "sitemaps").mkdir()
    (tmp_path / "sitemaps" / "skysmart.json").write_text("{not json")
    assert await FileSitemapCache(tmp_path).get("skysmart") is None
