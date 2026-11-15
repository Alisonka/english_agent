"""generate_topic: a curriculum topic in, the whole TopicContent out (research + plan + steps + summary).

This is the step-based replacement for `pipeline.generate_lesson`. It researches the topic once,
plans it into steps, writes each step and its re-teach variant, and writes the cheat sheet, all in
one span so a trace shows where the night's generation spends its time. Any stage raising leaves
the caller to record a failed content row and regenerate from scratch next time (no partial content).
"""

from __future__ import annotations

from contextlib import AsyncExitStack

import httpx
from anthropic import AsyncAnthropic

from english_agent.config import Settings
from english_agent.llm import AnthropicLLM, make_client
from english_agent.models.cefr import Cefr
from english_agent.models.curriculum import CurriculumTopic
from english_agent.models.topic import LessonStep, TopicContent
from english_agent.planner import plan_topic
from english_agent.research.agent import ResearchAgent
from english_agent.research.models import ResearchResult
from english_agent.research.sitemaps import FileSitemapCache, SitemapCache, SitemapIndex
from english_agent.research.tools import LiveResearchTools
from english_agent.step_writer import write_step
from english_agent.summary_writer import write_summary
from english_agent.tracing import get_tracer


def _research_http(settings: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=settings.http_timeout_seconds,
        headers={"User-Agent": settings.http_user_agent},
        follow_redirects=True,
    )


async def generate_topic(
    topic: CurriculumTopic,
    settings: Settings,
    *,
    cefr: Cefr = "A2",
    curriculum_version: int | None = None,
    client: AsyncAnthropic | None = None,
    sitemaps: SitemapCache | None = None,
) -> tuple[ResearchResult, TopicContent | None]:
    """The research result and, if a page was found, the whole topic content built from it."""
    async with AsyncExitStack() as stack:
        http = await stack.enter_async_context(_research_http(settings))
        if client is None:
            client = await stack.enter_async_context(make_client(settings))
        cache = sitemaps or FileSitemapCache(settings.cache_dir, ttl_days=settings.sitemap_ttl_days)
        tools = LiveResearchTools(http, SitemapIndex(http, cache), page_char_limit=settings.research_page_char_limit)
        researcher = ResearchAgent(
            AnthropicLLM(settings, model=settings.research_model, client=client),
            tools,
            max_steps=settings.research_max_steps,
            max_fetches_per_site=settings.research_max_fetches_per_site,
            curriculum_version=curriculum_version,
            cefr=cefr,
        )
        research = await researcher.run(topic)
        if research.notes is None:
            return research, None

        notes = research.notes
        writer = AnthropicLLM(settings, model=settings.writer_model, client=client)
        with get_tracer().start_as_current_span("topic.generate") as span:
            span.set_attribute("topic.id", topic.id)
            plan = await plan_topic(writer, topic, notes, cefr=cefr, curriculum_version=curriculum_version)
            span.set_attribute("topic.steps", len(plan.steps))

            steps: list[LessonStep] = []
            for index in range(1, len(plan.steps) + 1):
                steps.append(
                    await write_step(
                        writer,
                        plan,
                        index,
                        notes,
                        variant="main",
                        content_so_far=steps,
                        cefr=cefr,
                        curriculum_version=curriculum_version,
                    )
                )
            alternatives: list[LessonStep] = []
            for index in range(1, len(plan.steps) + 1):
                alternatives.append(
                    await write_step(
                        writer,
                        plan,
                        index,
                        notes,
                        variant="alt",
                        main=steps[index - 1],
                        content_so_far=steps[: index - 1],
                        cefr=cefr,
                        curriculum_version=curriculum_version,
                    )
                )
            summary = await write_summary(writer, plan, steps, notes, cefr=cefr, curriculum_version=curriculum_version)
            content = TopicContent(
                topic_id=topic.id,
                cefr=cefr,
                plan=plan,
                steps=steps,
                alternatives=alternatives,
                summary=summary,
                sources=[notes.source],
            )
        return research, content
