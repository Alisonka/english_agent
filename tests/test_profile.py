from __future__ import annotations

from datetime import UTC, datetime, timedelta

from english_agent.models.practice import TranslationGrade
from english_agent.models.storage import ErrorRecord, ItemRecord, ProfileInputs
from english_agent.profile import build_profile, refresh_profile
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


def test_refresh_appends_only_when_something_changed() -> None:
    stores, learners, _ = fake_stores()
    learner = learners.get_or_create("42", "A2")
    profiles = fake_profiles(stores)

    first = refresh_profile(profiles, learner.id, NOW)
    assert first is not None and first.version == 1
    assert refresh_profile(profiles, learner.id, NOW) is None  # nothing new

    profiles.next_inputs = ProfileInputs(cefr="A2", window_days=7, quiz_answers=3, quiz_correct=2)
    second = refresh_profile(profiles, learner.id, NOW)
    assert second is not None and second.version == 2
    assert second.profile.stats.quiz_accuracy == 0.667


def test_refresh_keeps_the_reflectors_notes() -> None:
    stores, learners, _ = fake_stores()
    learner = learners.get_or_create("42", "A2")
    profiles = fake_profiles(stores)
    first = refresh_profile(profiles, learner.id, NOW)
    assert first is not None
    profiles.versions[learner.id][-1] = first.model_copy(
        update={"profile": first.profile.model_copy(update={"notes_ru": "Путает there is и it is."})}
    )

    profiles.next_inputs = ProfileInputs(cefr="A2", window_days=7, lessons_sent=1)
    updated = refresh_profile(profiles, learner.id, NOW)

    assert updated is not None and updated.profile.notes_ru == "Путает there is и it is."
