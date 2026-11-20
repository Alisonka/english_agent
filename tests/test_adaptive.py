from __future__ import annotations

from english_agent.adaptive import AdaptiveTest, Candidate

POOL = [Candidate(f"{topic}:{n}", topic, d) for topic in ("a", "b") for n, d in enumerate([-2.0, -1.0, 0.0, 1.0, 2.0])]


def test_the_next_question_follows_the_estimate_and_covers_every_topic() -> None:
    test = AdaptiveTest(ability=0.0, topics=["a", "b"])
    first = test.next(POOL)
    assert first is not None and first.difficulty == 0.0 and first.topic_id == "a"
    test.record(first, correct=True)
    second = test.next(POOL)
    assert second is not None and second.topic_id == "b"  # the least answered topic comes next
    test.record(second, correct=True)
    harder = test.next(POOL)
    assert harder is not None and harder.difficulty > 0  # right answers: harder questions


def test_it_stops_by_settling_or_by_the_limits_and_never_repeats() -> None:
    test = AdaptiveTest(ability=0.0, topics=["a", "b"], max_total=6)
    asked: list[str] = []
    while (candidate := test.next(POOL)) is not None:
        asked.append(candidate.question_key)
        test.record(candidate, correct=candidate.difficulty <= 0.5)
    assert len(asked) == len(set(asked)) and len(asked) <= 6
    assert test.finished and all(test.per_topic(t) >= 2 for t in ("a", "b"))


def test_it_is_deterministic() -> None:
    def run() -> list[str]:
        test = AdaptiveTest(ability=0.3, topics=["a", "b"])
        keys = []
        while (candidate := test.next(POOL)) is not None:
            keys.append(candidate.question_key)
            test.record(candidate, correct=len(keys) % 3 != 0)
        return keys

    assert run() == run()
