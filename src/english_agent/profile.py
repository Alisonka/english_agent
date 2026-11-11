"""Building and refreshing the learner profile from what the database already knows.

`build_profile` is pure (inputs in, profile out) so the rules are unit-testable; `refresh_profile`
reads the inputs through the store and appends a new version only when something changed, so the
`learner_profiles` history stays meaningful.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime

from english_agent.models.profile import (
    MAX_ERROR_PATTERNS,
    MAX_WEAK_POINTS,
    PROFILE_WINDOW_DAYS,
    ErrorPattern,
    LearnerProfile,
    ProfileStats,
    WeakPoint,
)
from english_agent.models.storage import ErrorRecord, LearnerProfileRecord, ProfileInputs
from english_agent.storage.repositories import ProfileStore
from english_agent.versions import SCHEMA_VERSIONS, build_provenance


def _ratio(correct: int, total: int) -> float | None:
    return None if total == 0 else round(correct / total, 3)


def _weak_points(inputs: ProfileInputs) -> list[WeakPoint]:
    by_item: dict[int, list[ErrorRecord]] = {}
    for error in inputs.errors:
        if error.item_id is not None and error.item_id in inputs.items:
            by_item.setdefault(error.item_id, []).append(error)
    points = [
        WeakPoint(
            item_id=item_id,
            item_key=inputs.items[item_id].key,
            topic_id=inputs.items[item_id].topic_id,
            errors=len(errors),
            last_error_at=max(error.created_at for error in errors),
        )
        for item_id, errors in by_item.items()
    ]
    points.sort(key=lambda point: (-point.errors, -point.last_error_at.timestamp()))
    return points[:MAX_WEAK_POINTS]


def _error_patterns(inputs: ProfileInputs) -> list[ErrorPattern]:
    typed = [error for error in inputs.errors if error.error_type]
    counts = Counter(error.error_type for error in typed)
    latest = {error.error_type: error for error in sorted(typed, key=lambda error: error.created_at)}
    patterns = [
        ErrorPattern(
            error_type=str(error_type),
            count=count,
            example=f"{latest[error_type].original} -> {latest[error_type].corrected}",
        )
        for error_type, count in counts.most_common(MAX_ERROR_PATTERNS)
    ]
    return patterns


def build_profile(inputs: ProfileInputs) -> LearnerProfile:
    sentences = [sentence for grade in inputs.translation_grades for sentence in grade.sentences]
    rated = [sentence for sentence in sentences if sentence.verdict != "missing"]
    return LearnerProfile(
        cefr=inputs.cefr,
        window_days=inputs.window_days,
        weak_points=_weak_points(inputs),
        error_patterns=_error_patterns(inputs),
        stats=ProfileStats(
            lessons_sent=inputs.lessons_sent,
            quiz_answers=inputs.quiz_answers,
            quiz_accuracy=_ratio(inputs.quiz_correct, inputs.quiz_answers),
            translation_sentences=len(rated),
            translation_accuracy=_ratio(sum(s.verdict == "correct" for s in rated), len(rated)),
            cards=inputs.cards,
            due_cards=inputs.due_cards,
        ),
        notes_ru=inputs.notes_ru,
        provenance=build_provenance(component="profile", schema_version=SCHEMA_VERSIONS["learner_profile"]),
    )


def refresh_profile(profiles: ProfileStore, learner_id: int, now: datetime) -> LearnerProfileRecord | None:
    """Recompute the profile; store it as a new version if it differs from the latest one."""
    latest = profiles.latest(learner_id)
    inputs = profiles.inputs(learner_id, now, window_days=PROFILE_WINDOW_DAYS)
    if latest is not None:
        inputs = inputs.model_copy(update={"notes_ru": latest.profile.notes_ru})
    profile = build_profile(inputs)
    if latest is not None and latest.profile.same_as(profile):
        return None
    return profiles.append(learner_id, profile)
