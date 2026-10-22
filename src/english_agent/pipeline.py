"""Topic -> research -> lesson. The one place that wires the steps together.

    uv run python -m english_agent.pipeline --topic there_is_are
"""

from __future__ import annotations

import argparse
import sys

import httpx

from english_agent.config import Settings, get_settings
from english_agent.llm import AnthropicLLM
from english_agent.models.curriculum import CurriculumTopic, load_curriculum
from english_agent.models.lesson import Lesson
from english_agent.research.agent import ResearchAgent
from english_agent.research.models import ResearchResult
from english_agent.research.sitemaps import SitemapIndex
from english_agent.research.tools import LiveResearchTools
from english_agent.writer import write_lesson


def generate_lesson(topic: CurriculumTopic, settings: Settings) -> tuple[ResearchResult, Lesson | None]:
    """Returns the research result and, if a page was found, the lesson built from it."""
    with httpx.Client(
        timeout=settings.http_timeout_seconds,
        headers={"User-Agent": settings.http_user_agent},
        follow_redirects=True,
    ) as http:
        index = SitemapIndex(http, settings.cache_dir, ttl_days=settings.sitemap_ttl_days)
        tools = LiveResearchTools(http, index, page_char_limit=settings.research_page_char_limit)
        researcher = ResearchAgent(
            AnthropicLLM(settings, model=settings.research_model),
            tools,
            max_steps=settings.research_max_steps,
            max_fetches_per_site=settings.research_max_fetches_per_site,
        )
        research = researcher.run(topic)

    if research.notes is None:
        return research, None
    lesson = write_lesson(AnthropicLLM(settings, model=settings.writer_model), topic, research.notes)
    return research, lesson


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Research a topic and write the lesson.")
    parser.add_argument("--topic", required=True, help="topic id from curriculum/a2_topics.yaml")
    args = parser.parse_args(argv)

    topic = next((item for item in load_curriculum().topics if item.id == args.topic), None)
    if topic is None:
        print(f"unknown topic {args.topic!r}", file=sys.stderr)
        return 2

    research, lesson = generate_lesson(topic, get_settings())
    if lesson is None:
        print(research.message)
        return 1
    print(lesson.model_dump_json(indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
