"""When is what due: pure decisions over learners and the light state of their day.

The scheduler calls these every minute for every active learner and enqueues a job for each
"yes"; the job itself re-checks under a database claim, so a stale or duplicate "yes" costs a
no-op, never a second message. Everything here is a function of its arguments, so the rules
are tested across time zones and failure states without a database.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from english_agent.config import Settings
from english_agent.models.schedule import in_daily_window, local_now
from english_agent.models.storage import LearnerRecord, LessonState, PracticeKind

MAX_RETRY_BACKOFF_MINUTES = 60


def retry_backoff(attempts: int) -> timedelta:
    """Wait after a failed delivery attempt: 2, 4, 8 ... minutes, at most an hour."""
    return timedelta(minutes=min(2 ** max(attempts, 1), MAX_RETRY_BACKOFF_MINUTES))


def lesson_due(learner: LearnerRecord, state: LessonState | None, now: datetime, settings: Settings) -> bool:
    """Whether to (re)start the delivery of the learner's lesson for their local today.

    Inside [send time, send time + LESSON_CATCH_UP_HOURS) of the learner's local day, once the
    course has started, while the learner is active and not paused, and then:

    - no row yet: due;
    - sent or skipped: never again that day;
    - failed: after a backoff, while attempts are left (a Telegram outage, a crashed send);
    - pending or sending: when the claim was released or its worker looks dead.
    """
    if not learner.receives_lessons or learner.curriculum_started_on is None:
        return False
    local = local_now(learner.timezone, now)
    if local.date() < learner.curriculum_started_on:
        return False
    if not in_daily_window(local, learner.lesson_send_time, settings.lesson_catch_up_hours):
        return False
    if state is None:
        return True
    if state.status in ("sent", "skipped"):
        return False
    if state.claimed_at is None:
        return True
    if state.status == "failed":
        return state.attempts < settings.job_max_attempts and now >= state.claimed_at + retry_backoff(state.attempts)
    return now >= state.claimed_at + timedelta(minutes=settings.lesson_claim_stale_minutes)


def practice_due(
    learner: LearnerRecord,
    state: LessonState | None,
    claimed: set[PracticeKind],
    now: datetime,
    settings: Settings,
) -> bool:
    """Whether a practice block of today's lesson may be due: a cheap filter before the exact check.

    The job runs `practice.due_practice` on the full lesson (the gap after the quiz, whether the
    lesson has practice at all); this only spares a job for learners with nothing left to send.
    """
    if not learner.receives_lessons or state is None or state.status != "sent" or state.sent_at is None:
        return False
    local = local_now(learner.timezone, now)
    if local.time() >= settings.practice_cutoff_time:
        return False
    if "quiz" not in claimed:
        return now >= state.sent_at + timedelta(minutes=learner.quiz_delay_minutes)
    if "translation" not in claimed:
        return local.time() >= learner.translation_send_time
    return False
