from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from db import fresh_schema, run_alembic, schema_engine, search_path
from english_agent.models.lesson import Lesson
from english_agent.models.topic import TopicContent
from english_agent.storage import SessionFactory
from samples import (
    QUIZ,
    SOURCE,
    STEP_MESSAGE_PLURAL,
    STEP_MESSAGE_SINGULAR,
    TRANSLATION,
    step_quiz,
    step_translation,
)


@pytest.fixture(scope="session")
def database_url() -> str:
    """PostgreSQL for the storage tests; they are skipped when DATABASE_URL is not set."""
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        pytest.skip("DATABASE_URL is not set")
    return url


@pytest.fixture(scope="session")
def migrated_schema(database_url: str) -> Iterator[str]:
    """A throwaway schema migrated to head once per test session."""
    with fresh_schema(database_url) as schema, schema_engine(database_url, schema) as engine:
        run_alembic(engine, "upgrade", "head")
        yield schema


@pytest.fixture(scope="session")
async def async_engine(database_url: str, migrated_schema: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(database_url, connect_args=search_path(migrated_schema))
    yield engine
    await engine.dispose()


@pytest.fixture
async def sessions(async_engine: AsyncEngine) -> AsyncIterator[SessionFactory]:
    """Sessions inside one outer transaction that is rolled back after the test.

    Repository commits become savepoints, so every test starts from empty tables.
    """
    async with async_engine.connect() as connection:
        transaction = await connection.begin()
        try:
            yield async_sessionmaker(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
        finally:
            await transaction.rollback()


def lesson_payload(**overrides: Any) -> dict[str, Any]:
    """A valid `Lesson` payload; pass overrides to tweak individual fields."""
    payload: dict[str, Any] = {
        "topic_id": "there_is_are",
        "title_en": "there is / there are",
        "title_ru": "Конструкция there is / there are",
        "cefr": "A2",
        "key_idea_ru": (
            "Там, где по-русски «на столе книга» без глагола, по-английски нужен оборот there is / there are."
        ),
        "rule_ru": (
            "Конструкция there is / there are говорит, что где-то что-то есть или находится. There is "
            "используем с единственным числом и неисчисляемыми: There is a cat in the kitchen. There are — "
            "с множественным: There are two books on the table. В отрицании добавляем not: There isn't a bank "
            "here. В вопросе глагол выходит вперёд: Is there a bank near here? Отвечаем коротко: Yes, there is. "
            "No, there isn't. Переводим обычно с конца предложения, начиная с места."
        ),
        "form": "There is + singular noun / There are + plural noun",
        "nuances_ru": [
            "Если перечисляем несколько предметов, глагол согласуется с первым: There is a table and two chairs.",
            "С неисчисляемыми всегда there is: There is some milk in the fridge.",
            "There здесь не значит «там»; место указывается отдельно в конце: There is a shop there.",
        ],
        "examples": [
            {"en": "There is a cat in the kitchen.", "ru": "На кухне кот.", "highlights": ["There is"]},
            {"en": "There are two books on the table.", "ru": "На столе две книги.", "highlights": ["There are"]},
            {"en": "Is there a bank near here?", "ru": "Здесь рядом есть банк?", "highlights": ["Is there"]},
            {"en": "There isn't any milk.", "ru": "Молока нет.", "highlights": ["There isn't"]},
        ],
        "mistake_ru": "Калька с русского «это есть кот» даёт It is a cat вместо There is.",
        "incorrect": "It is a cat in the kitchen.",
        "correct": "There is a cat in the kitchen.",
        "sources": [
            {
                "url": "https://skysmart.ru/articles/english/konstrukciya-there-there-are",
                "title": "There is / are",
                "accessed_at": "2026-10-16",
            }
        ],
        "quiz": QUIZ,
        "translation": TRANSLATION,
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def lesson() -> Lesson:
    return Lesson.model_validate(lesson_payload())


def topic_content_payload(topic_id: str = "there_is_are", **overrides: Any) -> dict[str, Any]:
    """A valid two-step `TopicContent` payload; `topic_id` stamps the topic, overrides tweak fields."""
    plan = {
        "topic_id": "there_is_are",
        "cefr": "A2",
        "steps": [
            {
                "slug": "singular",
                "title_en": "there is",
                "title_ru": "there is — один предмет",
                "key_idea_ru": "Для одного предмета по-английски нужен оборот there is.",
                "rule_point_ids": [0, 1],
                "nuance_ids": [0],
                "example_ids": [0, 1],
                "mistake_ids": [0],
                "introduces_form": True,
            },
            {
                "slug": "plural",
                "title_en": "there are / questions",
                "title_ru": "there are и вопросы",
                "key_idea_ru": "Для многих предметов — there are, в вопросе глагол выходит вперёд.",
                "rule_point_ids": [2, 3],
                "nuance_ids": [1, 2],
                "example_ids": [2, 3],
                "mistake_ids": [],
                "introduces_form": True,
            },
        ],
        "characters": ["Anna", "Max"],
    }

    def main_step(slug: str, index: int, title_en: str, title_ru: str, message: dict[str, Any]) -> dict[str, Any]:
        return {
            "topic_id": "there_is_are",
            "title_en": title_en,
            "title_ru": title_ru,
            "cefr": "A2",
            "slug": slug,
            "index": index,
            "total": 2,
            "variant": "main",
            "message": message,
            "practice": {"quiz": step_quiz(slug), "translation": step_translation(slug)},
            "sources": [SOURCE],
        }

    def alt_step(slug: str, index: int, title_en: str, title_ru: str, message: dict[str, Any]) -> dict[str, Any]:
        return {
            "topic_id": "there_is_are",
            "title_en": title_en,
            "title_ru": title_ru,
            "cefr": "A2",
            "slug": slug,
            "index": index,
            "total": 2,
            "variant": "alt",
            "message": message,
            "practice": None,
            "sources": [SOURCE],
        }

    summary = {
        "topic_id": "there_is_are",
        "cefr": "A2",
        "title_en": "there is / there are",
        "title_ru": "there is / there are",
        "sections": [
            {"title_ru": "Один предмет", "lines_ru": ["there is + ед. число", "There is a cat here."]},
            {"title_ru": "Много и вопросы", "lines_ru": ["there are + мн. число", "Is there…? / Are there…?"]},
        ],
        "forms": ["There is + sing.", "There are + pl."],
        "top_mistakes_ru": ["It is a cat → There is a cat"],
        "sources": [SOURCE],
    }

    payload: dict[str, Any] = {
        "topic_id": "there_is_are",
        "cefr": "A2",
        "plan": plan,
        "steps": [
            main_step("singular", 1, "there is", "there is — один предмет", STEP_MESSAGE_SINGULAR),
            main_step("plural", 2, "there are / questions", "there are и вопросы", STEP_MESSAGE_PLURAL),
        ],
        "alternatives": [
            alt_step("singular", 1, "there is", "there is — один предмет", STEP_MESSAGE_SINGULAR),
            alt_step("plural", 2, "there are / questions", "there are и вопросы", STEP_MESSAGE_PLURAL),
        ],
        "summary": summary,
        "sources": [SOURCE],
    }
    if topic_id != "there_is_are":
        payload = json.loads(json.dumps(payload).replace("there_is_are", topic_id))
    payload.update(overrides)
    return payload


@pytest.fixture
def topic_content() -> TopicContent:
    return TopicContent.model_validate(topic_content_payload())
