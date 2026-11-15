from __future__ import annotations

from datetime import UTC, datetime, timedelta

from english_agent.llm import LLMUnavailable
from english_agent.models.practice import TranslationGrade
from english_agent.models.profile import FocusPoint, Reflection
from english_agent.models.storage import ErrorRecord, ItemRecord, ProfileInputs
from english_agent.profile import build_profile, current_reflection, refresh_profile
from english_agent.reflector import ReflectionRequest
from fakes import fake_profiles, fake_stores

NOW = datetime(2026, 11, 11, 20, 0, tzinfo=UTC)
GRADE = {
    "sentences": [
        {"n": 1, "learner_text": "ok", "verdict": "correct", "corrected": "ok", "explanation_ru": ""},
        {"n": 2, "learner_text": "bad", "verdict": "fixed", "corrected": "good", "explanation_ru": "Так надо."},
        {"n": 3, "learner_text": "", "verdict": "missing", "corrected": "ref", "explanation_ru": ""},
    ],
    "focus_ru": "Следи за формой.",
}


def item(item_id: int, topic_id: str) -> ItemRecord:
    return ItemRecord(
        id=item_id,
        key=f"{topic_id}:core",
        topic_id=topic_id,
        cefr="A2",
        form="f",
        function_ru="ф",
        explanation_ru="э",
        sources=[],
        version=1,
        source_lesson_id=None,
        created_at=NOW,
    )


def error(error_id: int, item_id: int | None, error_type: str | None, hours_ago: int) -> ErrorRecord:
    return ErrorRecord(
        id=error_id,
        learner_id=1,
        item_id=item_id,
        kind="quiz",
        original=f"wrong {error_id}",
        corrected=f"right {error_id}",
        error_type=error_type,
        explanation_ru=None,
        created_at=NOW - timedelta(hours=hours_ago),
    )


def test_profile_ranks_weak_points_and_error_patterns() -> None:
    inputs = ProfileInputs(
        cefr="A2",
        window_days=7,
        lessons_sent=5,
        errors=[
            error(1, 10, "gap_fill", 30),
            error(2, 10, "contrast", 2),
            error(3, 11, "gap_fill", 1),
            error(4, None, "gap_fill", 5),  # not linked to an item: counts as a pattern only
            error(5, 12, None, 5),  # item 12 is unknown to the inputs: ignored
        ],
        items={10: item(10, "there_is_are"), 11: item(11, "have_got")},
        quiz_answers=9,
        quiz_correct=6,
        translation_grades=[TranslationGrade.model_validate(GRADE)],
        cards=4,
        due_cards=1,
    )

    profile = build_profile(inputs)

    assert [(p.item_key, p.errors) for p in profile.weak_points] == [("there_is_are:core", 2), ("have_got:core", 1)]
    assert profile.weak_points[0].last_error_at == NOW - timedelta(hours=2)
    assert [(p.error_type, p.count) for p in profile.error_patterns] == [("gap_fill", 3), ("contrast", 1)]
    assert profile.error_patterns[0].example == "wrong 3 -> right 3"  # the latest one
    assert profile.stats.quiz_accuracy == round(6 / 9, 3)
    assert (profile.stats.translation_sentences, profile.stats.translation_accuracy) == (2, 0.5)  # missing not rated
    assert (profile.stats.cards, profile.stats.due_cards, profile.stats.lessons_sent) == (4, 1, 5)
    assert profile.provenance is not None and profile.provenance.component == "profile"


def test_empty_history_gives_no_accuracy() -> None:
    profile = build_profile(ProfileInputs(cefr="B1", window_days=7))
    assert profile.stats.quiz_accuracy is None and profile.stats.translation_accuracy is None
    assert profile.weak_points == [] and profile.error_patterns == []


async def test_refresh_appends_only_when_something_changed() -> None:
    stores, learners, _ = fake_stores()
    learner = await learners.get_or_create("42", "A2")
    profiles = fake_profiles(stores)

    first = await refresh_profile(stores, learner.id, NOW)
    assert first is not None and first.version == 1
    assert await refresh_profile(stores, learner.id, NOW) is None  # nothing new

    profiles.next_inputs = ProfileInputs(cefr="A2", window_days=7, quiz_answers=3, quiz_correct=2)
    second = await refresh_profile(stores, learner.id, NOW)
    assert second is not None and second.version == 2
    assert second.profile.stats.quiz_accuracy == 0.667


def reflection(at: datetime, until_error_id: int = 0) -> Reflection:
    point = FocusPoint(
        item_keys=["there_is_are:core"],
        pattern_ru="Путает there is и it is.",
        contrast_ru="There is — что-то есть где-то.",
        error_type="target_form",
        evidence_error_ids=[1],
    )
    return Reflection(focus_points=[point], based_on_errors=3, until_error_id=until_error_id, reflected_at=at)


def errors_on_one_item(count: int, first_id: int = 1) -> ProfileInputs:
    return ProfileInputs(
        cefr="A2",
        window_days=7,
        errors=[error(first_id + n, 7, "target_form", hours_ago=n + 1) for n in range(count)],
        items={7: item(7, "there_is_are")},
    )


async def test_refresh_keeps_the_reflection_when_none_is_due() -> None:
    stores, learners, _ = fake_stores()
    learner = await learners.get_or_create("42", "A2")
    profiles = fake_profiles(stores)
    first = await refresh_profile(stores, learner.id, NOW)
    assert first is not None
    kept = reflection(NOW - timedelta(hours=2))
    profiles.versions[learner.id][-1] = first.model_copy(
        update={"profile": first.profile.model_copy(update={"reflection": kept})}
    )

    async def must_not_run(request: ReflectionRequest) -> Reflection:
        raise AssertionError("a reflection two hours old is not redone")

    profiles.next_inputs = errors_on_one_item(5)
    updated = await refresh_profile(stores, learner.id, NOW, reflect=must_not_run)

    assert updated is not None and updated.profile.reflection == kept


async def test_new_errors_get_a_reflection_that_lands_in_the_profile() -> None:
    stores, learners, _ = fake_stores()
    learner = await learners.get_or_create("42", "A2")
    profiles = fake_profiles(stores)
    profiles.next_inputs = errors_on_one_item(3)
    seen: list[ReflectionRequest] = []

    async def reflect(request: ReflectionRequest) -> Reflection:
        seen.append(request)
        return reflection(request.now, until_error_id=3)

    record = await refresh_profile(stores, learner.id, NOW, reflect=reflect)

    assert record is not None and record.profile.reflection is not None
    assert record.profile.reflection.focus_keys == {"there_is_are:core"}
    [request] = seen
    assert request.previous is None and len(request.inputs.errors) == 3


async def test_a_failed_reflection_keeps_the_previous_one() -> None:
    stores, learners, _ = fake_stores()
    learner = await learners.get_or_create("42", "A2")
    profiles = fake_profiles(stores)
    first = await refresh_profile(stores, learner.id, NOW)
    assert first is not None
    old = reflection(NOW - timedelta(days=2))
    profiles.versions[learner.id][-1] = first.model_copy(
        update={"profile": first.profile.model_copy(update={"reflection": old})}
    )

    async def broken(request: ReflectionRequest) -> Reflection:
        raise LLMUnavailable("overloaded")

    profiles.next_inputs = errors_on_one_item(4, first_id=10)
    updated = await refresh_profile(stores, learner.id, NOW, reflect=broken)

    assert updated is not None and updated.profile.reflection == old


def test_only_a_current_reflection_steers_anything() -> None:
    profile = build_profile(ProfileInputs(cefr="A2", window_days=7, reflection=reflection(NOW - timedelta(days=15))))
    assert current_reflection(profile, NOW) is None
    fresh = profile.model_copy(update={"reflection": reflection(NOW - timedelta(days=1))})
    assert current_reflection(fresh, NOW) == fresh.reflection
    assert current_reflection(None, NOW) is None
