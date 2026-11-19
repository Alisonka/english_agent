"""The prerequisite graph over every level's topics, and the learner's frontier on it.

The links live in `curriculum/prerequisites.yaml`, versioned on their own (the topics' `version`
keys the shared content). Loading checks what the course relies on: every id exists, a topic only
requires topics of its own or a lower level, and there are no cycles. The frontier is what a
learner can open next: topics not mastered yet whose prerequisites all are.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import yaml

from english_agent.models.cefr import CEFR_LEVELS, Cefr
from english_agent.models.curriculum import Curriculum, PrerequisiteFile, load_curriculum
from english_agent.paths import available_cefr_levels, prerequisites_path


class TopicGraphError(ValueError):
    """The prerequisite file does not describe a valid graph over the curriculum."""


@dataclass(frozen=True)
class TopicNode:
    id: str
    cefr: Cefr
    order: int
    prerequisites: tuple[str, ...]


@dataclass(frozen=True)
class TopicGraph:
    version: int
    nodes: dict[str, TopicNode]  # in learning order: level, then the topic's order

    def prerequisites(self, topic_id: str) -> tuple[str, ...]:
        return self.nodes[topic_id].prerequisites

    def frontier(self, mastered: Iterable[str], *, cefr: Cefr | None = None) -> list[str]:
        """Topics (of `cefr`, or of every level) not mastered whose prerequisites all are, in learning order."""
        done = set(mastered)
        return [
            node.id
            for node in self.nodes.values()
            if node.id not in done
            and (cefr is None or node.cefr == cefr)
            and all(prerequisite in done for prerequisite in node.prerequisites)
        ]


def build_graph(curricula: Iterable[Curriculum], links: PrerequisiteFile) -> TopicGraph:
    """Join the topics with their links; raises `TopicGraphError` naming every problem found."""
    rank = {cefr: index for index, cefr in enumerate(CEFR_LEVELS)}
    topics: dict[str, tuple[Cefr, int]] = {}
    problems: list[str] = []
    for curriculum in sorted(curricula, key=lambda item: rank[item.cefr]):
        for topic in sorted(curriculum.topics, key=lambda item: item.order):
            if topic.id in topics:
                problems.append(f"topic {topic.id!r} is in both {topics[topic.id][0]} and {curriculum.cefr}")
            topics[topic.id] = (curriculum.cefr, topic.order)

    for topic_id, prerequisites in links.prerequisites.items():
        if topic_id not in topics:
            problems.append(f"unknown topic {topic_id!r}")
            continue
        if len(set(prerequisites)) != len(prerequisites):
            problems.append(f"{topic_id!r} lists a prerequisite twice")
        for prerequisite in prerequisites:
            if prerequisite == topic_id:
                problems.append(f"{topic_id!r} requires itself")
            elif prerequisite not in topics:
                problems.append(f"{topic_id!r} requires unknown topic {prerequisite!r}")
            elif rank[topics[prerequisite][0]] > rank[topics[topic_id][0]]:
                level, required = topics[topic_id][0], topics[prerequisite][0]
                problems.append(f"{topic_id!r} ({level}) requires {prerequisite!r} of a higher level ({required})")
    if problems:
        raise TopicGraphError("; ".join(problems))

    nodes = {
        topic_id: TopicNode(topic_id, cefr, order, tuple(links.prerequisites.get(topic_id, ())))
        for topic_id, (cefr, order) in topics.items()
    }
    cycle = _find_cycle(nodes)
    if cycle:
        raise TopicGraphError("prerequisite cycle: " + " -> ".join(cycle))
    return TopicGraph(version=links.version, nodes=nodes)


def _find_cycle(nodes: dict[str, TopicNode]) -> list[str]:
    """One cycle as a path that starts and ends at the same topic; empty when there is none."""
    state: dict[str, int] = {}  # 1: on the current path, 2: fully explored
    path: list[str] = []

    def visit(topic_id: str) -> list[str]:
        state[topic_id] = 1
        path.append(topic_id)
        for prerequisite in nodes[topic_id].prerequisites:
            if state.get(prerequisite) == 1:
                return path[path.index(prerequisite) :] + [prerequisite]
            if prerequisite not in state and (found := visit(prerequisite)):
                return found
        path.pop()
        state[topic_id] = 2
        return []

    for topic_id in nodes:
        if topic_id not in state and (found := visit(topic_id)):
            return found
    return []


def load_graph(path: Path | None = None) -> TopicGraph:
    """The graph over every curriculum file on disk and `curriculum/prerequisites.yaml`."""
    target = path or prerequisites_path()
    links = PrerequisiteFile.model_validate(yaml.safe_load(target.read_text(encoding="utf-8")))
    curricula = [load_curriculum(cefr) for cefr in CEFR_LEVELS if cefr in available_cefr_levels()]
    return build_graph(curricula, links)
