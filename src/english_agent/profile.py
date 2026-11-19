"""Building and refreshing the learner profile from what the database already knows.

`build_profile` is pure (inputs in, profile out) so the rules are unit-testable; `refresh_profile`
reads the inputs through the store, lets the Reflector read the new errors when a reflection is
due, and appends a new version only when something changed, so the `learner_profiles` history
stays meaningful.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime

from english_agent.llm import LLMError, LLMWriteError
from english_agent.metrics import REFLECTIONS
from english_agent.models.profile import (
    MAX_ERROR_PATTERNS,
    MAX_WEAK_POINTS,
    PROFILE_WINDOW_DAYS,
    ErrorPattern,
    FocusPoint,
    LearnerProfile,
    ProfileStats,
    Reflection,
    TopicWeakPoint,
    WeakPoint,
)
from english_agent.models.storage import ErrorRecord, ItemAccuracy, LearnerProfileRecord, ProfileInputs
from english_agent.models.topic import LessonStep
from english_agent.reflector import ReflectionRequest, Reflector, reflection_due
from english_agent.storage.repositories import Stores
from english_agent.versions import SCHEMA_VERSIONS, build_provenance

log = logging.getLogger(__name__)


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
        reflection=inputs.reflection,
        provenance=build_provenance(component="profile", schema_version=SCHEMA_VERSIONS["learner_profile"]),
    )


def weak_points_for_topic(
    steps: list[LessonStep],
    accuracy: dict[str, ItemAccuracy],
    errors: dict[str, list[ErrorRecord]],
    focus: list[FocusPoint] | None = None,
) -> list[TopicWeakPoint]:
    """The steps of a finished topic the learner struggled with, weakest first (for the cheat sheet).

    A step counts as weak if it has any error or any wrong review. `accuracy` and `errors` are keyed
    by item key (`<topic>:<slug>`); a current Reflector `focus` point on a step adds its hint. An
    empty result means the topic went without a stumble.
    """
    hints = {key: point.contrast_ru for point in reversed(focus or []) for key in point.item_keys}
    points: list[TopicWeakPoint] = []
    for step in steps:
        measured = accuracy.get(step.item_key)
        step_errors = errors.get(step.item_key, [])
        ratio = measured.ratio if measured is not None else None
        if not step_errors and not (ratio is not None and ratio < 1.0):
            continue
        example = None
        if step_errors:
            latest = max(step_errors, key=lambda error: error.created_at)
            example = f"{latest.original} -> {latest.corrected}"
        points.append(
            TopicWeakPoint(
                title_ru=step.title_ru,
                key_idea_ru=step.message.key_idea_ru,
                accuracy=ratio,
                example=example,
                contrast_ru=hints.get(step.item_key),
            )
        )
    points.sort(key=lambda point: (point.accuracy is None, point.accuracy if point.accuracy is not None else 0.0))
    return points


async def refresh_profile(
    stores: Stores, learner_id: int, now: datetime, *, reflect: Reflector | None = None
) -> LearnerProfileRecord | None:
    """Recompute the profile (and the reflection, when one is due); store it as a new version if it changed."""
    profiles = stores.profiles
    latest = await profiles.latest(learner_id)
    inputs = await profiles.inputs(learner_id, now, window_days=PROFILE_WINDOW_DAYS)
    reflection = latest.profile.reflection if latest is not None else None
    if reflect is not None and reflection_due(reflection, inputs.errors, now):
        reflection = await _reflect(stores, learner_id, inputs, reflection, now, reflect)
    profile = build_profile(inputs.model_copy(update={"reflection": reflection}))
    if latest is not None and latest.profile.same_as(profile):
        return None
    return await profiles.append(learner_id, profile)


async def _reflect(
    stores: Stores,
    learner_id: int,
    inputs: ProfileInputs,
    previous: Reflection | None,
    now: datetime,
    reflect: Reflector,
) -> Reflection | None:
    """A new reflection, or the previous one when the Reflector fails (logged, never raised)."""
    accuracy = await stores.cards.item_accuracy(learner_id, list(inputs.items))
    try:
        request = ReflectionRequest(
            inputs=inputs, accuracy=accuracy, previous=previous, now=now, learner_id=learner_id, reader=stores.evidence
        )
        reflection = await reflect(request)
    except (LLMError, LLMWriteError, ValueError) as error:
        REFLECTIONS.labels("failed").inc()
        log.warning("reflection failed for learner %s: %s: %s", learner_id, type(error).__name__, error)
        return previous
    REFLECTIONS.labels("written").inc()
    log.info("reflection for learner %s: %d focus point(s)", learner_id, len(reflection.focus_points))
    return reflection


def current_reflection(profile: LearnerProfile | None, now: datetime) -> Reflection | None:
    """The profile's reflection while it is current: what the grader, the Planner and the cheat sheet use."""
    reflection = profile.reflection if profile is not None else None
    return reflection if reflection is not None and reflection.is_current(now) else None
