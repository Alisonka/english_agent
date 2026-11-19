"""Rules of the Mentor's memory that code, not the model, applies.

- An expectation's outcome is computed from the answers in its window (`evaluate_expectation`).
- A hypothesis moves open -> probing -> confirmed | refuted, and goes stale after two weeks
  without new evidence (`can_move`, `is_stale`).
- The journal is folded into a short dossier by one model call (`write_dossier`), so a session
  reads the dossier and the fresh entries, not the whole history.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from english_agent.llm import ChatLLM, submit_model, tool_schema
from english_agent.models.mentor import (
    CLOSED_HYPOTHESES,
    Dossier,
    DossierDraft,
    Expectation,
    ExpectationOutcome,
    Hypothesis,
    HypothesisStatus,
    JournalEntry,
)
from english_agent.versions import SCHEMA_VERSIONS, build_provenance

STALE_AFTER = timedelta(days=14)
MAX_DOSSIER_ENTRIES = 60  # the newest entries folded in one call

_PROMPT = (Path(__file__).resolve().parent / "prompts" / "dossier_system.md").read_text(encoding="utf-8")
_MOVES: dict[HypothesisStatus, frozenset[HypothesisStatus]] = {
    "open": frozenset({"probing", "confirmed", "refuted", "stale"}),
    "probing": frozenset({"confirmed", "refuted", "stale"}),
}


@dataclass(frozen=True)
class Observation:
    """One graded answer in an expectation's window."""

    correct: bool
    error_type: str | None = None


def evaluate_expectation(expectation: Expectation, observations: list[Observation]) -> ExpectationOutcome:
    if len(observations) < expectation.min_evidence:
        return "insufficient"
    if expectation.metric == "accuracy":
        value = sum(observation.correct for observation in observations) / len(observations)
    else:
        wrong = [
            observation
            for observation in observations
            if not observation.correct
            and (expectation.error_type is None or observation.error_type == expectation.error_type)
        ]
        value = len(wrong) / len(observations)
    met = value >= expectation.threshold if expectation.comparison == "at_least" else value <= expectation.threshold
    return "met" if met else "not_met"


def can_move(current: HypothesisStatus, target: HypothesisStatus) -> bool:
    return target in _MOVES.get(current, frozenset())


def is_stale(hypothesis: Hypothesis, now: datetime) -> bool:
    return hypothesis.status not in CLOSED_HYPOTHESES and now - hypothesis.last_evidence_at >= STALE_AFTER


async def write_dossier(
    llm: ChatLLM, learner_id: int, entries: list[JournalEntry], previous: Dossier | None, now: datetime
) -> Dossier:
    """Fold the previous dossier and the newer journal entries into a new dossier."""
    if not entries:
        raise ValueError("nothing to fold: no journal entries after the previous dossier")
    entries = sorted(entries, key=lambda entry: entry.id)[-MAX_DOSSIER_ENTRIES:]
    provenance = build_provenance(
        component="dossier",
        schema_version=SCHEMA_VERSIONS["dossier"],
        model=getattr(llm, "model", None),
        prompt_names=("dossier_system",),
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _PROMPT},
        {"role": "user", "content": _brief(entries, previous)},
    ]

    def build(arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            **arguments,
            "learner_id": learner_id,
            "until_entry_id": entries[-1].id,
            "created_at": now,
            "provenance": provenance,
        }

    return await submit_model(
        llm,
        Dossier,
        tool=tool_schema(
            DossierDraft,
            name="submit_dossier",
            description="Submit the dossier: what is known about the learner, what was tried, what helped.",
        ),
        messages=messages,
        build=build,
        span_name="mentor.dossier",
        span_attrs={"dossier.entries": len(entries)},
    )


def _brief(entries: list[JournalEntry], previous: Dossier | None) -> str:
    lines: list[str] = []
    if previous is not None:
        lines += ["Previous dossier:", previous.summary_ru, ""]
        lines += [f"Tried: {item}" for item in previous.tried_ru]
        lines += [f"Helped: {item}" for item in previous.helped_ru]
        lines.append("")
    lines.append("Journal entries since then, oldest first (outcome is computed by code from the answers):")
    for entry in entries:
        outcome = entry.outcome or "not checked yet"
        lines.append(f"- {entry.created_at:%Y-%m-%d}: {entry.decision} Because: {entry.reason} Outcome: {outcome}.")
    return "\n".join(lines)
