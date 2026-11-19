from __future__ import annotations

import pytest

from english_agent.models.cefr import CEFR_LEVELS, Cefr
from english_agent.models.curriculum import Curriculum, PrerequisiteFile
from english_agent.topic_graph import TopicGraphError, build_graph, load_graph

FILLER = 40  # a curriculum holds at least 40 topics


def curriculum(cefr: Cefr, *ids: str) -> Curriculum:
    names = [*ids, *(f"{cefr.lower()}_filler_{n}" for n in range(FILLER - len(ids)))]
    return Curriculum.model_validate(
        {
            "version": 1,
            "cefr": cefr,
            "explanation_language": "ru",
            "example_language": "en",
            "topics": [
                {"id": name, "title_en": name, "title_ru": name, "order": order, "tags": ["t"]}
                for order, name in enumerate(names, start=1)
            ],
        }
    )


LEVELS = [curriculum("A2", "form", "irregular", "contrast"), curriculum("B1", "perfect_b1")]


def links(prerequisites: dict[str, list[str]]) -> PrerequisiteFile:
    return PrerequisiteFile(version=3, prerequisites=prerequisites)


def test_frontier_moves_as_prerequisites_are_mastered() -> None:
    graph = build_graph(
        LEVELS, links({"form": ["irregular"], "contrast": ["form"], "perfect_b1": ["contrast", "irregular"]})
    )

    assert graph.version == 3
    assert graph.prerequisites("perfect_b1") == ("contrast", "irregular")
    a2 = graph.frontier([], cefr="A2")
    assert "irregular" in a2 and "form" not in a2 and "contrast" not in a2
    assert graph.frontier(["irregular"], cefr="A2")[:1] == ["form"]
    assert "perfect_b1" not in graph.frontier(["irregular", "form"])
    assert "perfect_b1" in graph.frontier(["irregular", "form", "contrast"], cefr="B1")
    assert "irregular" not in graph.frontier(["irregular"])  # mastered topics leave the frontier


def test_a_cycle_is_reported_with_its_path() -> None:
    with pytest.raises(TopicGraphError, match=r"cycle: (form -> contrast -> form|contrast -> form -> contrast)"):
        build_graph(LEVELS, links({"form": ["contrast"], "contrast": ["form"]}))


@pytest.mark.parametrize(
    ("prerequisites", "message"),
    [
        ({"form": ["nope"]}, "'form' requires unknown topic 'nope'"),
        ({"nope": ["form"]}, "unknown topic 'nope'"),
        ({"form": ["form"]}, "'form' requires itself"),
        ({"form": ["irregular", "irregular"]}, "lists a prerequisite twice"),
        ({"form": ["perfect_b1"]}, "'form' \\(A2\\) requires 'perfect_b1' of a higher level \\(B1\\)"),
    ],
)
def test_broken_links_are_named(prerequisites: dict[str, list[str]], message: str) -> None:
    with pytest.raises(TopicGraphError, match=message):
        build_graph(LEVELS, links(prerequisites))


def test_a_topic_id_in_two_levels_is_rejected() -> None:
    with pytest.raises(TopicGraphError, match="'form' is in both A2 and B1"):
        build_graph([LEVELS[0], curriculum("B1", "form")], links({}))


def test_the_real_graph_loads_and_every_level_has_an_entry_point() -> None:
    graph = load_graph()

    assert graph.frontier([], cefr="A2")  # a new learner can start
    for level, cefr in enumerate(CEFR_LEVELS[1:], start=1):
        below = [node.id for node in graph.nodes.values() if CEFR_LEVELS.index(node.cefr) < level]
        assert graph.frontier(below, cefr=cefr), f"{cefr} opens once the levels below are mastered"
