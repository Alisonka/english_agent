"""CLI: research one curriculum topic against the live sites.

uv run python -m english_agent.research --topic present_perfect_vs_past_simple
"""

from __future__ import annotations

import argparse
import asyncio
import logging

import httpx

from english_agent.config import Settings, get_settings
from english_agent.llm import AnthropicLLM, make_client
from english_agent.logs import configure_logging
from english_agent.models.curriculum import CurriculumTopic, load_curriculum
from english_agent.research.agent import ResearchAgent
from english_agent.research.models import ResearchResult
from english_agent.research.sitemaps import FileSitemapCache, SitemapIndex
from english_agent.research.tools import LiveResearchTools

log = logging.getLogger(__name__)


async def _research(settings: Settings, topic: CurriculumTopic, model: str | None, version: int) -> ResearchResult:
    async with (
        httpx.AsyncClient(
            timeout=settings.http_timeout_seconds,
            headers={"User-Agent": settings.http_user_agent},
            follow_redirects=True,
        ) as http,
        make_client(settings) as client,
    ):
        index = SitemapIndex(http, FileSitemapCache(settings.cache_dir, ttl_days=settings.sitemap_ttl_days))
        tools = LiveResearchTools(http, index, page_char_limit=settings.research_page_char_limit)
        agent = ResearchAgent(
            AnthropicLLM(settings, model=model, client=client),
            tools,
            max_steps=settings.research_max_steps,
            max_fetches_per_site=settings.research_max_fetches_per_site,
            curriculum_version=version,
        )
        return await agent.run(topic)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the research subagent for one topic.")
    parser.add_argument("--topic", required=True, help="topic id from curriculum/a2_topics.yaml")
    parser.add_argument("--model", default=None, help="Claude model id (overrides RESEARCH_MODEL)")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings)
    curriculum = load_curriculum()
    topic = next((item for item in curriculum.topics if item.id == args.topic), None)
    if topic is None:
        log.error("unknown topic %r", args.topic)
        return 2

    result = asyncio.run(_research(settings, topic, args.model, curriculum.version))
    print(result.model_dump_json(indent=2, ensure_ascii=False))
    return 0 if result.status != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
