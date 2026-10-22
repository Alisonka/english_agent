"""Deterministic tests of the research loop: scripted LLM, in-memory tools, no network."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pytest

from english_agent.llm import AssistantTurn, ToolCall
from english_agent.models.curriculum import CurriculumTopic
from english_agent.research.agent import ResearchAgent
from english_agent.research.models import NOT_FOUND_MESSAGE, PageContent, SearchHit
from english_agent.research.sites import SITE_BY_NAME, Site
from english_agent.research.tools import rank_urls

TOPIC = CurriculumTopic(
    id="present_perfect_vs_past_simple",
    title_en="Present Perfect vs Past Simple",
    title_ru="Present Perfect и Past Simple",
    order=43,
    tags=["tenses"],
)

ENGLEX_URL = "https://englex.ru/present-perfect-vs-past-simple/"
SKYENG_URL = "https://skyeng.ru/articles/present-perfect-i-past-simple/"
SKYSMART_URL = "https://skysmart.ru/articles/english/present-perfect-tense"

GOOD_NOTES: dict[str, Any] = {
    "source": {"url": ENGLEX_URL, "title": "whatever the model says", "accessed_at": "1999-01-01"},
    "summary_ru": "Статья объясняет разницу между Present Perfect и Past Simple и показывает, когда время известно.",
    "rule_points": [
        "Past Simple — действие в известное время в прошлом.",
        "Present Perfect — результат к настоящему моменту.",
        "С yesterday, ago, last year используется Past Simple.",
    ],
    "examples": [{"en": "I saw him yesterday.", "ru": "Я видел его вчера."}, {"en": "I have seen this film."}],
    "typical_mistakes": ["I have seen him yesterday."],
}


class FakeTools:
    def __init__(self, urls: dict[str, list[str]], pages: dict[str, str]) -> None:
        self.urls = urls
        self.pages = pages
        self.fetched: list[str] = []

    def search_site(self, site: Site, keywords: list[str], limit: int = 5) -> list[SearchHit]:
        return rank_urls(self.urls.get(site.name, []), keywords, limit)

    def fetch_page(self, url: str) -> PageContent:
        self.fetched.append(url)
        return PageContent(url=url, title=f"Title of {url}", text=self.pages[url], truncated=False)


class ScriptedLLM:
    def __init__(self, turns: list[AssistantTurn]) -> None:
        self._turns = list(turns)
        self.transcripts: list[list[dict[str, Any]]] = []

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> AssistantTurn:
        self.transcripts.append(list(messages))
        if not self._turns:
            raise AssertionError("LLM asked for more turns than scripted")
        return self._turns.pop(0)


def call(name: str, **arguments: Any) -> AssistantTurn:
    return AssistantTurn(content=None, tool_calls=[ToolCall(id=f"call_{name}_{len(arguments)}", name=name, arguments=arguments)])


def search(site: str, *keywords: str) -> AssistantTurn:
    return call("search_site", site=site, keywords=list(keywords))


def fetch(url: str) -> AssistantTurn:
    return call("fetch_page", url=url)


def finish(found: bool, notes: dict[str, Any] | None = None) -> AssistantTurn:
    arguments: dict[str, Any] = {"found": found}
    if notes is not None:
        arguments["notes"] = notes
    return call("finish", **arguments)


def make_agent(turns: list[AssistantTurn], tools: FakeTools, **kwargs: Any) -> tuple[ResearchAgent, ScriptedLLM]:
    llm = ScriptedLLM(turns)
    agent = ResearchAgent(llm, tools, today=date(2026, 10, 13), **kwargs)
    return agent, llm


def all_sites_tools() -> FakeTools:
    return FakeTools(
        urls={"englex": [ENGLEX_URL], "skyeng": [SKYENG_URL], "skysmart": [SKYSMART_URL]},
        pages={ENGLEX_URL: "Present Perfect vs Past Simple ...", SKYENG_URL: "...", SKYSMART_URL: "..."},
    )


def last_tool_result(llm: ScriptedLLM) -> str:
    return llm.transcripts[-1][-1]["content"]


# -- happy path ----------------------------------------------------------------------


def test_found_on_first_site_grounds_source_in_fetched_page() -> None:
    tools = all_sites_tools()
    agent, _ = make_agent(
        [search("englex", "present", "perfect"), fetch(ENGLEX_URL), finish(True, GOOD_NOTES)], tools
    )
    result = agent.run(TOPIC)

    assert result.status == "found"
    assert result.sites_checked == ["englex"]
    assert result.notes is not None
    assert result.notes.site == "englex"
    assert str(result.notes.source.url) == ENGLEX_URL
    # Provenance comes from the fetched page, not from the model.
    assert result.notes.source.title == f"Title of {ENGLEX_URL}"
    assert result.notes.source.accessed_at == date(2026, 10, 13)
    assert result.notes.page_title == f"Title of {ENGLEX_URL}"
    assert [record.tool for record in result.trajectory] == ["search_site", "fetch_page", "finish"]
    assert all(record.ok for record in result.trajectory)


def test_falls_through_to_third_site() -> None:
    tools = FakeTools(
        urls={"englex": ["https://englex.ru/idioms-about-food/"], "skyeng": [], "skysmart": [SKYSMART_URL]},
        pages={SKYSMART_URL: "Present Perfect ..."},
    )
    notes = {**GOOD_NOTES, "source": {"url": SKYSMART_URL, "title": "x", "accessed_at": "2026-10-13"}}
    agent, _ = make_agent(
        [
            search("englex", "present", "perfect"),
            search("skyeng", "present", "perfect"),
            search("skysmart", "present", "perfect"),
            fetch(SKYSMART_URL),
            finish(True, notes),
        ],
        tools,
    )
    result = agent.run(TOPIC)
    assert result.status == "found"
    assert result.notes is not None and result.notes.site == "skysmart"
    assert result.sites_checked == ["englex", "skyeng", "skysmart"]


def test_not_found_on_any_site_returns_exact_message() -> None:
    tools = FakeTools(urls={"englex": [], "skyeng": [], "skysmart": []}, pages={})
    agent, _ = make_agent(
        [search("englex", "present"), search("skyeng", "present"), search("skysmart", "present"), finish(False)],
        tools,
    )
    result = agent.run(TOPIC)
    assert result.status == "not_found"
    assert result.message == NOT_FOUND_MESSAGE
    assert result.notes is None
    assert result.sites_checked == ["englex", "skyeng", "skysmart"]


# -- rules enforced by code, not by the prompt -------------------------------------------


def test_site_order_is_enforced() -> None:
    tools = all_sites_tools()
    agent, llm = make_agent(
        [search("skysmart", "present"), search("englex", "present"), fetch(ENGLEX_URL), finish(True, GOOD_NOTES)],
        tools,
    )
    result = agent.run(TOPIC)
    assert result.status == "found"
    first = result.trajectory[0]
    assert first.tool == "search_site" and not first.ok
    assert "search englex before skysmart" in first.summary
    assert "error" in llm.transcripts[1][-1]["content"]


def test_cannot_declare_not_found_before_checking_all_sites() -> None:
    tools = all_sites_tools()
    agent, llm = make_agent(
        [search("englex", "nothing"), finish(False), search("skyeng", "present"), search("skysmart", "present"), finish(False)],
        tools,
    )
    result = agent.run(TOPIC)
    assert result.status == "not_found"
    rejected = [record for record in result.trajectory if record.tool == "finish" and not record.ok]
    assert len(rejected) == 1
    assert "skyeng, skysmart" in rejected[0].summary


def test_fetch_only_urls_offered_by_search_and_inside_allowlist() -> None:
    tools = all_sites_tools()
    agent, _ = make_agent(
        [
            search("englex", "present", "perfect"),
            fetch("https://evil.example.com/present-perfect/"),
            fetch("https://englex.ru/some-other-post/"),
            fetch(ENGLEX_URL),
            finish(True, GOOD_NOTES),
        ],
        tools,
    )
    result = agent.run(TOPIC)
    assert result.status == "found"
    summaries = [record.summary for record in result.trajectory if record.tool == "fetch_page"]
    assert summaries[0] == "URL is outside the allowed sites"
    assert summaries[1] == "only fetch URLs returned by search_site"
    assert tools.fetched == [ENGLEX_URL]


def test_finish_rejects_source_that_was_not_fetched() -> None:
    tools = all_sites_tools()
    bad_notes = {**GOOD_NOTES, "source": {"url": SKYENG_URL, "title": "x", "accessed_at": "2026-10-13"}}
    agent, _ = make_agent(
        [search("englex", "present", "perfect"), fetch(ENGLEX_URL), finish(True, bad_notes), finish(True, GOOD_NOTES)],
        tools,
    )
    result = agent.run(TOPIC)
    assert result.status == "found"
    rejected = [record for record in result.trajectory if record.tool == "finish" and not record.ok]
    assert rejected and "must be a page you fetched" in rejected[0].summary


def test_fetch_cap_per_site() -> None:
    urls = [f"https://englex.ru/post-{i}-present/" for i in range(3)]
    tools = FakeTools(urls={"englex": urls, "skyeng": [], "skysmart": []}, pages={url: "text" for url in urls})
    notes = {**GOOD_NOTES, "source": {"url": urls[0], "title": "x", "accessed_at": "2026-10-13"}}
    agent, _ = make_agent(
        [search("englex", "present"), fetch(urls[0]), fetch(urls[1]), fetch(urls[2]), finish(True, notes)],
        tools,
        max_fetches_per_site=2,
    )
    result = agent.run(TOPIC)
    assert result.status == "found"
    assert tools.fetched == urls[:2]
    capped = [record for record in result.trajectory if record.tool == "fetch_page" and not record.ok]
    assert capped and "fetch limit reached" in capped[0].summary


def test_budget_exhaustion_is_failed_not_not_found() -> None:
    tools = all_sites_tools()
    agent, _ = make_agent([search("englex", "present")] * 3, tools, max_steps=3, max_searches_per_site=5)
    result = agent.run(TOPIC)
    assert result.status == "failed"
    assert "budget exhausted" in result.message
    assert result.steps == 3


def test_prose_without_tools_is_nudged_and_counts_as_step() -> None:
    tools = all_sites_tools()
    agent, llm = make_agent(
        [AssistantTurn(content="Let me think..."), search("englex", "present", "perfect"), fetch(ENGLEX_URL), finish(True, GOOD_NOTES)],
        tools,
    )
    result = agent.run(TOPIC)
    assert result.status == "found"
    assert result.steps == 4
    assert "Use a tool" in llm.transcripts[1][-1]["content"]


def test_page_text_is_marked_untrusted() -> None:
    tools = all_sites_tools()
    agent, llm = make_agent([search("englex", "present", "perfect"), fetch(ENGLEX_URL), finish(True, GOOD_NOTES)], tools)
    agent.run(TOPIC)
    fetch_result = json.loads(llm.transcripts[2][-1]["content"])
    assert fetch_result["text"].startswith('<page_content untrusted="true">')


# -- search ranking -------------------------------------------------------------------


def test_rank_urls_prefers_more_keyword_matches_then_shorter_slug() -> None:
    urls = [
        "https://skysmart.ru/articles/english/present-perfect-continuous",
        "https://skysmart.ru/articles/english/present-perfect-tense",
        "https://skysmart.ru/articles/english/present-simple-tense",
        "https://skysmart.ru/articles/english/glagol-to-sing",
    ]
    hits = rank_urls(urls, ["present", "perfect"], limit=5)
    assert [hit.slug for hit in hits] == ["present-perfect-tense", "present-perfect-continuous", "present-simple-tense"]
    assert hits[0].score == 2


def test_rank_urls_ignores_tiny_keywords() -> None:
    urls = ["https://englex.ru/it-slang/", "https://englex.ru/articles-a-an-the/"]
    hits = rank_urls(urls, ["a", "an", "is", "articles"], limit=5)
    assert [hit.slug for hit in hits] == ["articles-a-an-the"]
    assert rank_urls(urls, ["a", "an"], limit=5) == []


def test_rank_urls_matches_transliterated_slugs() -> None:
    urls = ["https://skysmart.ru/articles/english/artikli-v-anglijskom-yazyke"]
    assert rank_urls(urls, ["articles", "artikli"], limit=5)[0].score == 1
    assert rank_urls(urls, ["present"], limit=5) == []


@pytest.mark.parametrize(
    ("url", "site", "expected"),
    [
        ("https://skysmart.ru/articles/english/present-simple-tense", "skysmart", True),
        ("https://skysmart.ru/articles/study/how-to-learn", "skysmart", False),
        ("https://skyeng.ru/magazine/present-simple/", "skyeng", True),
        ("https://englex.ru/present-simple/", "englex", True),
        ("https://englex.ru.evil.com/present-simple/", "englex", False),
    ],
)
def test_site_article_allowlist(url: str, site: str, expected: bool) -> None:
    assert SITE_BY_NAME[site].is_article(url) is expected
