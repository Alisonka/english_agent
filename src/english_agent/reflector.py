"""When a learner's errors are worth a fresh diagnosis, and the Diagnostician that writes it.

It runs inside the profile refresh (after a finished quiz or a graded translation), at most once
every `MIN_GAP` per learner and only when there are new errors to read (`reflection_due`): enough
of them, or one after a long quiet stretch. The Diagnostician (`english_agent.diagnostician`, on
REFLECTOR_MODEL) returns the profile's `reflection`, which the grader (explanations), the Planner
(review slots) and the cheat sheet (hints) read while it is current. A failed run is logged and the
previous reflection is kept.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from anthropic import AsyncAnthropic

from english_agent.config import Settings
from english_agent.diagnostician import EvidenceReader, diagnose
from english_agent.llm import AnthropicLLM
from english_agent.models.profile import Reflection
from english_agent.models.storage import ErrorRecord, ItemAccuracy, ProfileInputs
from english_agent.topic_graph import default_graph

MIN_GAP = timedelta(hours=20)  # at most about one reflection a day per learner
QUIET_GAP = timedelta(days=7)  # after this long, even one new error is worth a fresh look
NEW_ERRORS = 3  # new errors that call for a reflection sooner


@dataclass(frozen=True)
class ReflectionRequest:
    """What one reflection reads: the profile's window of errors and items, accuracy, the previous reflection."""

    inputs: ProfileInputs
    accuracy: dict[int, ItemAccuracy]
    previous: Reflection | None
    now: datetime
    learner_id: int | None = None
    reader: EvidenceReader | None = None  # the Diagnostician's history tools; None: errors only


Reflector = Callable[[ReflectionRequest], Awaitable[Reflection]]


def reflection_due(previous: Reflection | None, errors: list[ErrorRecord], now: datetime) -> bool:
    """Whether the errors since the last reflection are worth a new one."""
    if previous is not None and now - previous.reflected_at < MIN_GAP:
        return False
    new = [
        error
        for error in errors
        if error.item_id is not None and (previous is None or error.id > previous.until_error_id)
    ]
    if len(new) >= NEW_ERRORS:
        return True
    return previous is not None and bool(new) and now - previous.reflected_at >= QUIET_GAP


def make_reflector(settings: Settings, client: AsyncAnthropic | None = None) -> Reflector | None:
    """Reflect with REFLECTOR_MODEL through the process's Claude client; None when the Reflector is off."""
    if not settings.reflector_model:
        return None
    llm = AnthropicLLM(settings, model=settings.reflector_model, effort=settings.reflector_effort, client=client)

    async def run(request: ReflectionRequest) -> Reflection:
        return await diagnose(
            llm,
            learner_id=request.learner_id,
            inputs=request.inputs,
            accuracy=request.accuracy,
            previous=request.previous,
            now=request.now,
            reader=request.reader,
            graph=default_graph(),
        )

    return run
