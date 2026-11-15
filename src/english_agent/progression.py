"""Where a learner is in the course: which step comes next, and whether a step needs re-teaching.

Pure functions over the learner's sent steps (`SentStep`) and their per-item accuracy, so the
rules are tested without a database. `next_step` replaces `next_topic` for the step-based course:
it advances within a topic, inserts a re-teach when a step is not sticking, and signals when the
learner is leaving a finished topic (so its cheat sheet can be sent).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from english_agent.models.curriculum import Curriculum, CurriculumTopic
from english_agent.models.storage import ItemAccuracy, SentStep, StepVariant

RETEACH_THRESHOLD = 0.6
RETEACH_MIN_CHECKS = 2
RETEACH_MIN_DAYS = 2


@dataclass(frozen=True)
class TopicProgress:
    """One topic's sent steps for a learner, in send order, with the content they were pinned to."""

    topic_id: str
    step_count: int
    sent: list[SentStep] = field(default_factory=list)
    content_id: int | None = None


@dataclass(frozen=True)
class NextStep:
    """What to send next: a step of a topic, and whether a finished topic is being left behind."""

    topic: CurriculumTopic
    index: int
    variant: StepVariant
    content_id: int | None  # the pinned content for a topic in progress; None starts a new topic
    leaving: str | None  # a now-finished topic whose cheat sheet is owed, or None


def topic_progress(rows: list[SentStep]) -> dict[str, TopicProgress]:
    """Group sent steps by topic, keeping order and the content the topic's first step was pinned to."""
    progress: dict[str, TopicProgress] = {}
    for row in rows:
        existing = progress.get(row.topic_id)
        if existing is None:
            progress[row.topic_id] = TopicProgress(
                topic_id=row.topic_id, step_count=row.step_count, sent=[row], content_id=row.content_id
            )
        else:
            existing.sent.append(row)
    return progress


def is_done(progress: TopicProgress) -> bool:
    """Whether every step index of the topic has a main row (legacy lessons are one step of one)."""
    main_indices = {row.step_index for row in progress.sent if row.variant == "main"}
    return all(index in main_indices for index in range(1, progress.step_count + 1))


def reteach_candidate(
    progress: TopicProgress,
    accuracy: dict[str, ItemAccuracy],
    *,
    threshold: float = RETEACH_THRESHOLD,
    min_checks: int = RETEACH_MIN_CHECKS,
) -> str | None:
    """The slug of the earliest main step that is not sticking and has no re-teach yet, or None.

    Not sticking = checked at least `min_checks` times over at least `RETEACH_MIN_DAYS` days with
    accuracy below `threshold`. At most one re-teach per step (an existing alt row disqualifies it).
    """
    reteached = {row.step_slug for row in progress.sent if row.variant == "alt"}
    for row in sorted((r for r in progress.sent if r.variant == "main"), key=lambda r: r.step_index):
        if row.step_slug in reteached:
            continue
        accuracy_of = accuracy.get(row.item_key)
        if (
            accuracy_of is not None
            and accuracy_of.checks >= min_checks
            and accuracy_of.days >= RETEACH_MIN_DAYS
            and accuracy_of.ratio is not None
            and accuracy_of.ratio < threshold
        ):
            return row.step_slug
    return None


def _first_missing_main_index(progress: TopicProgress) -> int:
    sent = {row.step_index for row in progress.sent if row.variant == "main"}
    return next(index for index in range(1, progress.step_count + 1) if index not in sent)


def _index_of(progress: TopicProgress, slug: str) -> int:
    return next(row.step_index for row in progress.sent if row.variant == "main" and row.step_slug == slug)


def next_step(
    curriculum: Curriculum,
    progress: dict[str, TopicProgress],
    skipped: set[str],
    accuracy: dict[str, ItemAccuracy],
) -> NextStep | None:
    """The learner's next step, or None once every topic is done or skipped.

    A re-teach of the current (or just-finished) topic is preferred over advancing. Starting a new
    topic carries `leaving` = the topic just finished, so the caller can send its cheat sheet.
    """
    ordered = sorted(curriculum.topics, key=lambda topic: topic.order)
    last_done: CurriculumTopic | None = None
    for topic in ordered:
        if topic.id in skipped:
            continue
        here = progress.get(topic.id)
        if here is not None and is_done(here):
            last_done = topic
            continue
        if here is not None:
            slug = reteach_candidate(here, accuracy)
            if slug is not None:
                return NextStep(topic, _index_of(here, slug), "alt", here.content_id, None)
            return NextStep(topic, _first_missing_main_index(here), "main", here.content_id, None)
        # A new topic: first honour a re-teach owed on the topic just finished.
        owed = _owed_reteach(last_done, progress, accuracy)
        if owed is not None:
            return owed
        return NextStep(topic, 1, "main", None, last_done.id if last_done is not None else None)
    return _owed_reteach(last_done, progress, accuracy)


def _owed_reteach(
    last_done: CurriculumTopic | None, progress: dict[str, TopicProgress], accuracy: dict[str, ItemAccuracy]
) -> NextStep | None:
    if last_done is None:
        return None
    here = progress[last_done.id]
    slug = reteach_candidate(here, accuracy)
    if slug is None:
        return None
    return NextStep(last_done, _index_of(here, slug), "alt", here.content_id, None)
