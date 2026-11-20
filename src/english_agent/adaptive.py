"""Adaptive question selection, shared by the exam and the placement test.

The next question is chosen by code from the current ability estimate: the topic with the fewest
answers among those still open (so every group is covered), then the unseen question whose
difficulty is closest to the estimate (a right answer makes the next one harder). Each answer moves
the estimate by an Elo step that shrinks as answers accumulate. A topic closes once it has
`min_per_topic` answers and the estimate has settled, or `max_per_topic`; the test ends when every
topic is closed or `max_total` is reached. Deterministic: ties break by question key.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from english_agent.knowledge import probability

START_K = 0.8
MIN_K = 0.2


@dataclass(frozen=True)
class Candidate:
    question_key: str
    topic_id: str
    difficulty: float


@dataclass
class AdaptiveTest:
    ability: float
    topics: list[str]
    min_per_topic: int = 2
    max_per_topic: int = 4
    max_total: int = 25
    settle: float = 0.15  # |last move| below this, with enough answers: the topic is settled
    answers: list[tuple[Candidate, bool]] = field(default_factory=list)
    _last_move: dict[str, float] = field(default_factory=dict)

    def per_topic(self, topic_id: str) -> int:
        return sum(1 for candidate, _ in self.answers if candidate.topic_id == topic_id)

    def topic_open(self, topic_id: str) -> bool:
        count = self.per_topic(topic_id)
        if count >= self.max_per_topic:
            return False
        settled = count >= self.min_per_topic and abs(self._last_move.get(topic_id, 1.0)) < self.settle
        return not settled

    @property
    def finished(self) -> bool:
        return len(self.answers) >= self.max_total or not any(self.topic_open(t) for t in self.topics)

    def next(self, pool: list[Candidate]) -> Candidate | None:
        """The next question, or None when the test is over or nothing fitting is left."""
        if self.finished:
            return None
        seen = {candidate.question_key for candidate, _ in self.answers}
        open_topics = sorted((t for t in self.topics if self.topic_open(t)), key=lambda t: (self.per_topic(t), t))
        for topic in open_topics:
            fitting = [c for c in pool if c.topic_id == topic and c.question_key not in seen]
            if fitting:
                return min(fitting, key=lambda c: (abs(c.difficulty - self.ability), c.question_key))
        return None

    def record(self, candidate: Candidate, correct: bool) -> float:
        """Count an answer; returns the move of the ability estimate."""
        k = max(MIN_K, START_K / (1 + len(self.answers) / 4))
        move = k * ((1.0 if correct else 0.0) - probability(self.ability, [], candidate.difficulty))
        self.ability += move
        self._last_move[candidate.topic_id] = move
        self.answers.append((candidate, correct))
        return move

    def accuracy(self, topic_id: str) -> float | None:
        marks = [correct for candidate, correct in self.answers if candidate.topic_id == topic_id]
        return sum(marks) / len(marks) if marks else None
