"""Research subagent: topic in, grounded TopicNotes (or "нет на сайте") out.

Reasoning loop (ReAct with tool calling):

    system prompt + topic
      -> model proposes a tool call
      -> code VALIDATES it against the rules below, EXECUTES it, appends the result
      -> repeat until `finish` or the step budget runs out

The model decides keywords, which URL looks promising, and whether a page really
covers the topic. The code owns everything that must not depend on model mood:
site order, step/fetch caps, URL allowlist, grounding of the final source.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import httpx
from opentelemetry.trace import Span, Status, StatusCode
from pydantic import ValidationError

from english_agent.llm import ChatLLM, ToolCall
from english_agent.models.cefr import Cefr
from english_agent.models.curriculum import CurriculumTopic
from english_agent.research.models import (
    NOT_FOUND_MESSAGE,
    PageContent,
    ResearchResult,
    ToolCallRecord,
    TopicNotes,
)
from english_agent.research.sites import SITE_BY_NAME, SITE_ORDER, site_for_url
from english_agent.research.tools import ResearchTools, tool_specs
from english_agent.tracing import get_tracer
from english_agent.versions import SCHEMA_VERSIONS, Provenance, build_provenance

_PROMPT = (Path(__file__).resolve().parents[1] / "prompts" / "research_system.md").read_text(encoding="utf-8")


class ToolError(Exception):
    """Rule violation or tool failure. Reported back to the model, never raised out of the loop."""


@dataclass  # в dataclass аннотация типа не проверяется во время выполнения
class _State:
    searches: dict[str, int] = field(default_factory=dict)  # счётчики по имени сайта (для лимитов)
    fetches: dict[str, int] = field(default_factory=dict)
    offered_urls: set[str] = field(default_factory=set)  # URL, которые уже предложены
    fetched: dict[str, PageContent] = field(default_factory=dict)  # кеш страниц
    trajectory: list[ToolCallRecord] = field(default_factory=list)  # история вызовов инструментов

    @property
    def sites_checked(self) -> list[str]:
        return [name for name in SITE_ORDER if name in self.searches]  # список сайтов, которые уже проверены

    def unsearched_sites(self) -> list[str]:
        return [name for name in SITE_ORDER if name not in self.searches]  # список сайтов, которые ещё не проверены


class ResearchAgent:
    def __init__(
        self,
        llm: ChatLLM,
        tools: ResearchTools,
        *,
        max_steps: int = 12,
        max_fetches_per_site: int = 2,
        max_searches_per_site: int = 3,
        today: date | None = None,
        curriculum_version: int | None = None,
        cefr: Cefr = "A2",
    ) -> None:
        self._llm = llm
        self._tools = tools
        self._max_steps = max_steps
        self._max_fetches_per_site = max_fetches_per_site
        self._max_searches_per_site = max_searches_per_site
        self._today = today or date.today()
        self._curriculum_version = curriculum_version
        self._cefr = cefr
        self._system_prompt = _PROMPT
        self._tool_specs = tool_specs()

    def _provenance(self) -> Provenance:
        return build_provenance(
            component="research",
            schema_version=SCHEMA_VERSIONS["research_result"],
            model=getattr(self._llm, "model", None),
            prompt_names=("research_system",),
            curriculum_version=self._curriculum_version,
        )

    # -- public ------------------------------------------------------------------

    async def run(self, topic: CurriculumTopic) -> ResearchResult:
        with get_tracer().start_as_current_span("research.run") as span:
            span.set_attribute("topic.id", topic.id)
            result = await self._run_loop(topic)
            span.set_attribute("research.status", result.status)
            span.set_attribute("research.steps", result.steps)
            span.set_attribute("research.sites_checked", result.sites_checked)
            if result.status == "failed":
                span.set_status(Status(StatusCode.ERROR, result.message))
            return result

    async def _run_loop(self, topic: CurriculumTopic) -> ResearchResult:
        state = _State()
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_prompt},
            {"role": "user", "content": self._topic_message(topic)},
        ]

        for step in range(1, self._max_steps + 1):
            turn = await self._llm.chat(messages, self._tool_specs)
            messages.append(turn.to_message())

            if not turn.tool_calls:
                messages.append(
                    {
                        "role": "user",
                        "content": "Use a tool: search_site, fetch_page, or finish. Do not answer in prose.",
                    }
                )
                continue

            for call in turn.tool_calls:
                with get_tracer().start_as_current_span(f"tool.{call.name}") as tool_span:
                    tool_span.set_attribute("research.step", step)
                    if call.name == "finish":
                        tool_span.set_attribute("finish.found", bool(call.arguments.get("found")))
                        try:
                            return self._finish(topic, call, state, step)
                        except ToolError as error:
                            self._record(state, step, call, ok=False, summary=str(error))
                            messages.append(self._tool_message(call, {"error": str(error)}))
                            tool_span.record_exception(error)
                            tool_span.set_status(Status(StatusCode.ERROR, str(error)))
                            continue
                    try:
                        payload = await self._dispatch(call, state)
                        self._record(
                            state,
                            step,
                            call,
                            ok=True,
                            summary=self._summarize(call, payload),
                        )
                        self._annotate_tool_span(tool_span, call, payload)
                    except ToolError as error:
                        payload = {"error": str(error)}
                        self._record(state, step, call, ok=False, summary=str(error))
                        tool_span.record_exception(error)
                        tool_span.set_status(Status(StatusCode.ERROR, str(error)))
                    messages.append(self._tool_message(call, payload))

        return ResearchResult(
            topic_id=topic.id,
            status="failed",
            message=f"research budget exhausted after {self._max_steps} steps",
            sites_checked=state.sites_checked,
            steps=self._max_steps,
            trajectory=state.trajectory,
            provenance=self._provenance(),
        )

    # -- tool dispatch -------------------------------------------------------------

    async def _dispatch(self, call: ToolCall, state: _State) -> Any:
        if call.name == "search_site":
            return await self._search_site(call.arguments, state)
        if call.name == "fetch_page":
            return await self._fetch_page(call.arguments, state)
        raise ToolError(f"unknown tool {call.name!r}")

    async def _search_site(self, arguments: dict[str, Any], state: _State) -> list[dict[str, Any]]:
        site_name = arguments.get("site")
        keywords = arguments.get("keywords")
        if site_name not in SITE_BY_NAME:
            raise ToolError(f"site must be one of {list(SITE_ORDER)}")
        if not isinstance(keywords, list) or not keywords:
            raise ToolError("keywords must be a non-empty list of strings")

        position = SITE_ORDER.index(site_name)
        skipped = [name for name in SITE_ORDER[:position] if name not in state.searches]
        if skipped:
            raise ToolError(f"sites must be tried in order; search {skipped[0]} before {site_name}")
        if state.searches.get(site_name, 0) >= self._max_searches_per_site:
            raise ToolError(f"search limit reached for {site_name}; move to the next site or finish")

        state.searches[site_name] = state.searches.get(site_name, 0) + 1
        hits = await self._tools.search_site(SITE_BY_NAME[site_name], [str(keyword) for keyword in keywords])
        state.offered_urls.update(hit.url for hit in hits)
        return [hit.model_dump() for hit in hits]

    async def _fetch_page(self, arguments: dict[str, Any], state: _State) -> dict[str, Any]:
        url = str(arguments.get("url", "")).strip()
        site = site_for_url(url)
        if site is None or not site.is_article(url):
            raise ToolError("URL is outside the allowed sites")
        if url not in state.offered_urls:
            raise ToolError("only fetch URLs returned by search_site")
        if state.fetches.get(site.name, 0) >= self._max_fetches_per_site:
            raise ToolError(f"fetch limit reached for {site.name}; decide with what you have or move on")

        if url in state.fetched:
            page = state.fetched[url]
        else:
            try:
                page = await self._tools.fetch_page(url)
            except (httpx.HTTPError, ValueError) as error:
                raise ToolError(f"fetch failed: {error}") from error
            state.fetches[site.name] = state.fetches.get(site.name, 0) + 1
            state.fetched[url] = page

        return {
            "url": page.url,
            "title": page.title,
            "truncated": page.truncated,
            "text": f'<page_content untrusted="true">\n{page.text}\n</page_content>',  # prompt injection
        }

    def _finish(self, topic: CurriculumTopic, call: ToolCall, state: _State, step: int) -> ResearchResult:
        found = call.arguments.get("found")
        if not isinstance(found, bool):
            raise ToolError("finish requires boolean 'found'")

        if not found:
            missing = state.unsearched_sites()
            if missing:
                raise ToolError(f"cannot declare not found: you have not searched {', '.join(missing)}")
            self._record(state, step, call, ok=True, summary=NOT_FOUND_MESSAGE)
            return ResearchResult(
                topic_id=topic.id,
                status="not_found",
                message=NOT_FOUND_MESSAGE,
                sites_checked=state.sites_checked,
                steps=step,
                trajectory=state.trajectory,
                provenance=self._provenance(),
            )

        raw_notes = call.arguments.get("notes")
        if not isinstance(raw_notes, dict):
            raise ToolError("found=true requires 'notes'")
        raw_source = raw_notes.get("source")
        source = raw_source if isinstance(raw_source, dict) else {}
        url = str(source.get("url", "")).strip()
        page = state.fetched.get(url)
        if page is None:
            raise ToolError("notes.source.url must be a page you fetched with fetch_page")
        site = site_for_url(url)
        assert site is not None  # guaranteed by fetch_page allowlist

        # Code owns provenance fields: the model cannot mislabel site, title or date.
        raw_notes["site"] = site.name
        raw_notes["page_title"] = page.title
        raw_notes["source"] = {
            "url": url,
            "title": page.title,
            "accessed_at": self._today.isoformat(),
        }
        try:
            notes = TopicNotes.model_validate(raw_notes)
        except ValidationError as error:
            raise ToolError(f"notes failed validation: {error.errors()[:3]}") from error

        self._record(state, step, call, ok=True, summary=f"found on {site.name}: {url}")
        return ResearchResult(
            topic_id=topic.id,
            status="found",
            message=f"found on {site.name}",
            notes=notes,
            sites_checked=state.sites_checked,
            steps=step,
            trajectory=state.trajectory,
            provenance=self._provenance(),
        )

    # -- helpers -------------------------------------------------------------------

    def _topic_message(self, topic: CurriculumTopic) -> str:
        return (
            f"Topic id: {topic.id}\n"
            f"Title (en): {topic.title_en}\n"
            f"Title (ru): {topic.title_ru}\n"
            f"Level: {self._cefr}\n"
            f"Today: {self._today.isoformat()}\n"
            "Start with englex."
        )

    @staticmethod
    def _tool_message(call: ToolCall, payload: Any) -> dict[str, Any]:
        return {
            "role": "tool",
            "tool_call_id": call.id,
            "content": json.dumps(payload, ensure_ascii=False),
        }

    @staticmethod
    def _record(state: _State, step: int, call: ToolCall, *, ok: bool, summary: str) -> None:
        state.trajectory.append(
            ToolCallRecord(
                step=step,
                tool=call.name,
                arguments=call.arguments,
                ok=ok,
                summary=summary[:300],
            )
        )

    @staticmethod
    def _annotate_tool_span(span: Span, call: ToolCall, payload: Any) -> None:
        if call.name == "search_site":
            span.set_attribute("search.site", str(call.arguments.get("site")))
            if isinstance(payload, list):
                span.set_attribute("search.hits", len(payload))
        elif call.name == "fetch_page" and isinstance(payload, dict):
            span.set_attribute("fetch.url", str(payload.get("url", "")))
            span.set_attribute("fetch.chars", len(str(payload.get("text", ""))))
            span.set_attribute("fetch.truncated", bool(payload.get("truncated", False)))

    @staticmethod
    def _summarize(call: ToolCall, payload: Any) -> str:
        if call.name == "search_site":
            return f"{len(payload)} hits on {call.arguments.get('site')}"
        if call.name == "fetch_page":
            return f"fetched {payload['url']} ({len(payload['text'])} chars)"
        return call.name
