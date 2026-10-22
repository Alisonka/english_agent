"""CLI: research one curriculum topic against the live sites.

    uv run python -m english_agent.research --topic present_perfect_vs_past_simple
"""

from __future__ import annotations

import argparse
import sys

import httpx

from english_agent.config import get_settings
from english_agent.llm import AnthropicLLM
from english_agent.models.curriculum import load_curriculum
from english_agent.research.agent import ResearchAgent
from english_agent.research.sitemaps import SitemapIndex
from english_agent.research.tools import LiveResearchTools


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the research subagent for one topic.")
    parser.add_argument("--topic", required=True, help="topic id from curriculum/a2_topics.yaml")
    parser.add_argument("--model", default=None, help="Claude model id (overrides RESEARCH_MODEL)")
    args = parser.parse_args(argv)

    settings = get_settings()
    curriculum = load_curriculum()
    topic = next((item for item in curriculum.topics if item.id == args.topic), None)
    if topic is None:
        print(f"unknown topic {args.topic!r}", file=sys.stderr)
        return 2

    with httpx.Client(
        timeout=settings.http_timeout_seconds,
        headers={"User-Agent": settings.http_user_agent},
        follow_redirects=True,
    ) as http:
        index = SitemapIndex(http, settings.cache_dir, ttl_days=settings.sitemap_ttl_days)
        tools = LiveResearchTools(http, index, page_char_limit=settings.research_page_char_limit)
        agent = ResearchAgent(
            AnthropicLLM(settings, model=args.model),
            tools,
            max_steps=settings.research_max_steps,
            max_fetches_per_site=settings.research_max_fetches_per_site,
        )
        result = agent.run(topic)

    print(result.model_dump_json(indent=2, ensure_ascii=False))
    return 0 if result.status != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
