from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import update

from english_agent.llm import AssistantTurn, ToolCall
from english_agent.mentor_memory import Observation, can_move, evaluate_expectation, is_stale, write_dossier
from english_agent.models.mentor import Expectation, Hypothesis, JournalEntry
from english_agent.storage import (
    EvidenceRepo,
    InvalidCitationsError,
    LearnerRepo,
    MentorMemoryRepo,
    SessionFactory,
    session_scope,
)
from english_agent.storage.orm import ErrorRow, EvidenceRow, JournalEntryRow
from fakes import ScriptedLLM

NOW = datetime(2026, 11, 10, 20, 0, tzinfo=UTC)


def expectation(**changes: Any) -> Expectation:
    values: dict[str, Any] = {
        "topic_id": "present_perfect_vs_past_simple",
        "metric": "error_rate",
        "comparison": "at_most",
        "threshold": 0.34,
        "due_on": date(2026, 11, 13),
        "min_evidence": 3,
    }
    return Expectation.model_validate({**values, **changes})


# --- rules ------------------------------------------------------------------------


def test_an_expectation_is_met_not_met_or_insufficient() -> None:
    right, form = Observation(correct=True), Observation(correct=False, error_type="form")
    other = Observation(correct=False, error_type="tense")

    assert evaluate_expectation(expectation(), [right, form]) == "insufficient"
    assert evaluate_expectation(expectation(), [right, right, form]) == "met"  # 1/3 <= 0.34
    assert evaluate_expectation(expectation(), [right, form, other]) == "not_met"
    # Narrowed to form errors, the tense error does not count against it.
    assert evaluate_expectation(expectation(error_type="form"), [right, form, other]) == "met"
    accuracy = expectation(metric="accuracy", comparison="at_least", threshold=0.8)
    assert evaluate_expectation(accuracy, [right] * 4 + [form]) == "met"
    assert evaluate_expectation(accuracy, [right] * 3 + [form, form]) == "not_met"


def test_an_expectation_names_exactly_one_target() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        expectation(item_id=7)
    with pytest.raises(ValidationError, match="exactly one"):
        expectation(topic_id=None)
    with pytest.raises(ValidationError, match="error_rate only"):
        expectation(metric="accuracy", comparison="at_least", error_type="form")


def hypothesis(status: str, last_evidence_at: datetime) -> Hypothesis:
    return Hypothesis.model_validate(
        {
            "id": 1,
            "learner_id": 1,
            "statement_ru": "Не знает третью форму неправильных глаголов.",
            "topic_id": None,
            "item_ids": [],
            "evidence_ids": [1],
            "status": status,
            "opened_at": last_evidence_at,
            "last_evidence_at": last_evidence_at,
            "closed_at": None,
        }
    )


def test_hypothesis_moves_and_staleness() -> None:
    assert can_move("open", "probing") and can_move("probing", "confirmed") and can_move("open", "refuted")
    assert not can_move("probing", "open") and not can_move("confirmed", "refuted") and not can_move("stale", "open")

    assert is_stale(hypothesis("probing", NOW - timedelta(days=14)), NOW)
    assert not is_stale(hypothesis("probing", NOW - timedelta(days=13)), NOW)
    assert not is_stale(hypothesis("confirmed", NOW - timedelta(days=30)), NOW)  # closed ones stay as they are


def entry(entry_id: int, outcome: str | None) -> JournalEntry:
    return JournalEntry.model_validate(
        {
            "id": entry_id,
            "learner_id": 1,
            "decision": "Ремедиал по третьей форме.",
            "reason": "Ошибки формы в Present Perfect.",
            "evidence_ids": [1],
            "expectation": None,
            "outcome": outcome,
            "checked_at": None,
            "created_at": NOW,
        }
    )


async def test_the_dossier_gets_code_owned_fields_and_sees_outcomes() -> None:
    draft = {"summary_ru": "Путает форму глагола.", "tried_ru": ["Ремедиал"], "helped_ru": ["Ремедиал"]}
    llm = ScriptedLLM([AssistantTurn(content=None, tool_calls=[ToolCall("d", "submit_dossier", draft)])])

    dossier = await write_dossier(llm, 1, [entry(5, "met"), entry(3, None)], None, NOW)

    assert (dossier.learner_id, dossier.until_entry_id, dossier.created_at) == (1, 5, NOW)
    assert dossier.provenance.component == "dossier"
    brief = llm.calls[0]["messages"][1]["content"]
    assert "Outcome: met." in brief and "Outcome: not checked yet." in brief
    assert brief.index("Outcome: not checked yet.") < brief.index("Outcome: met.")  # oldest first
    with pytest.raises(ValueError, match="nothing to fold"):
        await write_dossier(llm, 1, [], None, NOW)


# --- storage ----------------------------------------------------------------------


async def seed(sessions: SessionFactory) -> tuple[int, int, list[int]]:
    """A learner (and another one) with four answers on one topic after NOW: right, form error, right, missing."""
    learner = (await LearnerRepo(sessions).get_or_create("42", "B1")).id
    other = (await LearnerRepo(sessions).get_or_create("43", "B1")).id
    topic = "present_perfect_vs_past_simple"
    async with session_scope(sessions) as session:
        error = ErrorRow(learner_id=learner, kind="text", original="I have went", corrected="I have gone")
        error.error_type = "form"
        session.add(error)
        await session.flush()
        rows = [
            EvidenceRow(
                learner_id=learner,
                kind="quiz_answer",
                source_ref="q:1",
                topic_id=topic,
                payload={"is_correct": True},
                occurred_at=NOW + timedelta(hours=1),
            ),
            EvidenceRow(
                learner_id=learner,
                kind="translation_sentence",
                source_ref="t:1",
                topic_id=topic,
                error_id=error.id,
                payload={"verdict": "fixed"},
                occurred_at=NOW + timedelta(days=1),
            ),
            EvidenceRow(
                learner_id=learner,
                kind="translation_sentence",
                source_ref="t:2",
                topic_id=topic,
                payload={"verdict": "correct"},
                occurred_at=NOW + timedelta(days=2),
            ),
            EvidenceRow(
                learner_id=learner,
                kind="translation_sentence",
                source_ref="t:3",
                topic_id=topic,
                payload={"verdict": "missing"},
                occurred_at=NOW + timedelta(days=2),
            ),
            EvidenceRow(
                learner_id=other,
                kind="quiz_answer",
                source_ref="q:other",
                topic_id=topic,
                payload={"is_correct": False},
                occurred_at=NOW + timedelta(days=1),
            ),
        ]
        session.add_all(rows)
        await session.flush()
        return learner, other, [row.id for row in rows]


async def test_journal_rejects_bad_citations_and_code_checks_due_expectations(sessions: SessionFactory) -> None:
    learner, _, ids = await seed(sessions)
    memory = MentorMemoryRepo(sessions)

    with pytest.raises(InvalidCitationsError) as rejected:
        await memory.add_entry(learner, decision="d", reason="r", evidence_ids=[ids[0], ids[4], 99999])
    assert rejected.value.ids == [ids[4], 99999]  # another learner's, and one that does not exist
    with pytest.raises(InvalidCitationsError):
        await memory.add_entry(learner, decision="d", reason="r", evidence_ids=[])

    written = await memory.add_entry(
        learner,
        decision="Ремедиал по третьей форме.",
        reason="Ошибки формы.",
        evidence_ids=[ids[1]],
        expectation=expectation(error_type="form", due_on=date(2026, 11, 12)),
    )
    await memory.add_entry(learner, decision="Без ожидания.", reason="r", evidence_ids=[ids[0]])
    async with session_scope(sessions) as session:
        # The entries were written before the answers they expect (those come after NOW).
        await session.execute(update(JournalEntryRow).values(created_at=NOW))

    assert await memory.check_due_expectations(date(2026, 11, 11), NOW) == 0  # not due yet
    assert await memory.check_due_expectations(date(2026, 11, 12), NOW) == 1
    assert await memory.check_due_expectations(date(2026, 11, 12), NOW) == 0  # checked once

    [checked, plain] = await memory.entries(learner)
    assert checked.id == written.id and (checked.outcome, checked.checked_at) == ("met", NOW)  # 1 form error in 3
    assert plain.outcome is None
    assert [e.id for e in await memory.entries(learner, after_id=written.id)] == [plain.id]


async def test_hypotheses_follow_the_rules_and_go_stale(sessions: SessionFactory) -> None:
    learner, other, ids = await seed(sessions)
    memory = MentorMemoryRepo(sessions)
    opened = await memory.open_hypothesis(learner, statement_ru="Форма глагола.", evidence_ids=[ids[1]], now=NOW)
    old = await memory.open_hypothesis(
        learner, statement_ru="Перенос с русского.", evidence_ids=[ids[0]], now=NOW - timedelta(days=20)
    )

    probing = await memory.move_hypothesis(learner, opened.id, "probing", evidence_ids=[ids[2]], now=NOW)
    assert (probing.status, probing.evidence_ids) == ("probing", [ids[1], ids[2]])
    with pytest.raises(ValueError, match="cannot move from probing to open"):
        await memory.move_hypothesis(learner, opened.id, "open", evidence_ids=[ids[2]], now=NOW)
    with pytest.raises(ValueError, match="no hypothesis"):
        await memory.move_hypothesis(other, opened.id, "refuted", evidence_ids=[ids[4]], now=NOW)

    assert await memory.close_stale(NOW) == 1
    assert [h.id for h in await memory.open_hypotheses(learner)] == [opened.id]
    confirmed = await memory.move_hypothesis(learner, opened.id, "confirmed", evidence_ids=[ids[2]], now=NOW)
    assert confirmed.closed_at == NOW and await memory.open_hypotheses(learner) == []
    assert old.status == "open"


async def test_the_newest_dossier_is_the_learners(sessions: SessionFactory) -> None:
    learner, _, _ = await seed(sessions)
    memory = MentorMemoryRepo(sessions)
    draft = {"summary_ru": "s", "tried_ru": [], "helped_ru": []}
    llm = ScriptedLLM([AssistantTurn(content=None, tool_calls=[ToolCall("d", "submit_dossier", draft)])] * 2)
    first = await write_dossier(llm, learner, [entry(1, None)], None, NOW)
    second = await write_dossier(llm, learner, [entry(2, "met")], first, NOW + timedelta(days=7))

    assert await memory.latest_dossier(learner) is None
    await memory.save_dossier(first)
    await memory.save_dossier(second)
    assert await memory.latest_dossier(learner) == second


async def test_topic_answers_count_graded_answers_but_not_left_out_sentences(sessions: SessionFactory) -> None:
    learner, other, _ = await seed(sessions)
    evidence = EvidenceRepo(sessions)
    topic = "present_perfect_vs_past_simple"
    assert await evidence.topic_answers(learner, [topic, "be_present"]) == {topic: (3, 2)}
    assert await evidence.topic_answers(other, [topic]) == {topic: (1, 0)}
    assert await evidence.topic_answers(learner, []) == {}
